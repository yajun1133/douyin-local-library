#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""追更核心：盯着重点作者，发现新作品就下载进库（Python 3.12 专用）。

为什么单独一个模块而不是塞进 server.py：
server 跑在 3.13，而 curl_cffi / ABogus 签名是 3.12 编译版，import 会 bad magic
number。所以这条链路跟 latest_helper.py / fetch_favorites.py 一样，只能在 3.12 里跑。
server 那边要么用子进程拉起本模块，要么由本模块自己常驻。

跟 DouK 的关系：
  不依赖它。DouK 是 GUI 程序，要人点「开始」；追更要求无人值守，所以这里直接把
  aweme/post 返回里的 play_addr / images 下下来，命名规则**故意跟 DouK 保持一致**
  （`日期-昵称-描述[_序号].ext`），这样 scanner 扫进来跟 DouK 下的没有任何区别。

判新（三层，缺一不可）：
  1. aweme_id 在 state.seen 里 → 处理过（下载成功或彻底失败）
  2. create_time <= state.last_ts → 上次扫描之前就有的老作品
  3. 目标文件已存在 / 同一天同描述的文件已在 → DouK 以前下过
第 1 层管「自己下过的」，第 3 层管「DouK 下过的」，两层合起来才不重复。
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

WATCH_FILE = os.path.join(paths.DATA_DIR, "watch.json")
STATE_FILE = os.path.join(paths.DATA_DIR, "watch_state.json")
LOG_FILE = os.path.join(paths.DATA_DIR, "watch.log")
# 逐条下载明细（jsonl，一行一条作品）。
# 为什么单独记：watch_state.json 里每个作者只有一个 downloads 计数，点进去没法看「下到了啥」。
# 主界面的「累计追更下载」要能点开看明细，就必须有一条一条的记录。
# 每行：{"ts": 落盘时间, "uid":, "nickname":, "date": 作品发布日, "desc":,
#        "rels": [库内相对路径…], "size": 字节, "backfill": 是否从老日志回填}
DOWNLOADS_FILE = os.path.join(paths.DATA_DIR, "watch_downloads.jsonl")
# 账号池：追更用的登录 cookie 可以放好几个，一个被限流就换下一个。
ACCOUNTS_FILE = os.path.join(paths.DATA_DIR, "watch_accounts.json")

DEFAULT_CFG = {
    # 检查节奏按「这位作者上次发作品距今多久」分四档。
    # 刚发过的更可能再发，查得勤；久没动静的放宽到一天一次。
    # 这样 200 位作者的日常请求量只有「全员 15 分钟」的 1/6 左右，见 tier_of()。
    "interval_hot": 900,        # 2 天内发过 → 15 分钟
    "interval_warm": 3600,      # 7 天内发过 → 1 小时
    "interval_cold": 10800,     # 30 天内发过 → 3 小时
    "interval_idle": 86400,     # 更久（或从没抓到过新作品）→ 24 小时
    "hot_days": 2,
    "warm_days": 7,
    "cold_days": 30,
    "per_author_pages": 1,      # 每个作者翻几页（每页 18~20 条）；1 页够覆盖绝大多数日更
    "backfill_pages": 4,        # 补漏时最多往下翻几页（只有"整页都比上次新"才会用上）
    # 「本地一件作品都没有」的作者，第一次要把现有作品整体补一遍 ——
    # 只翻 1 页只能看到最近 20 条，更老的永远补不到。这里给一个较深的上限，
    # 一页 20 条算，10 页 ≈ 200 条，够覆盖绝大多数账号；翻不满说明到底了。
    "bulk_pages": 10,
    "gap": 2.0,                 # 两次作者之间的间隔（秒），带抖动
    "gap_jitter": 0.3,
    # —— 节奏与账号安全 ——
    # 只用"别像机器"这一个原则：2 秒一次 + 抖动，等于一个人连着刷作者主页。
    # 号池里有几个号就天然分摊几份，所以上限不必卡太死。
    "daily_budget": 20000,      # 每天最多查多少次（全部号合计），到了就停到第二天
    "rest_every": 100,          # 每查 N 位作者歇一口气
    "rest_secs": 20,            # 歇多久（秒）
    "break_fails": 5,           # 连续 N 位查询都失败（换号也不行）→ 全部暂停
    "break_cool": 600,          # 首次冷却 10 分钟；再犯翻倍，最长 2 小时
    "net_wait": 20,             # 网络类失败时等多少秒再试（不封号、不熔断）
    "net_cool": 120,            # 连着 3 次网络不通 → 停 2 分钟，通了自动续上
    "account_ban": 300,         # 单个号连续失败后先歇 5 分钟，再犯翻倍
    "max_per_round": 40,        # 单轮最多下载多少条，防止一次性爆量
    # 追更内容：all=视频+图文都追；video=只追视频（图文整条跳过）；image=只追图文。
    # 用户 2026-11-08：「追更那里填一个追更更新的视频选项」—— 图文多的作者光图片就占一大半，
    # 想只要视频的人得能关掉。
    "content": "all",
    "desc_len": 64,             # 描述截断长度，对齐 DouK 的 desc_length
    "fail_limit": 3,            # 同一条作品失败 N 次后放弃，避免卡死
    "seen_keep": 400,           # 每个作者保留多少条 aweme_id 记忆
}

VIDEO_EXT = ".mp4"
IMG_EXT_BY_TYPE = {
    "image/jpeg": ".jpeg", "image/jpg": ".jpeg", "image/png": ".png",
    "image/webp": ".webp", "image/heic": ".heic", "image/gif": ".gif",
    "image/avif": ".avif",
}
VIDEO_TYPES = ("video/", "application/octet-stream")
# 判断「这一项本地已经有了」时认可的静态图后缀（动图 webp/gif 也算）
IMG_EXTS = (".jpeg", ".jpg", ".png", ".webp", ".gif", ".heic", ".avif")

# Windows 文件名不允许的字符。DouK 也没法保留这些，一律换成下划线。
BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

# curl_cffi 默认会读 HTTPS_PROXY / HTTP_PROXY。本机的会话代理端口**每个会话都换**，
# 常驻进程继承到的那个通常几小时后就没了 —— 于是所有请求都变成
#   curl: (7) Failed to connect to www.douyin.com:443 over proxy 127.0.0.1
# 抖音直连一直是通的，所以这里一律显式绕过代理（空串 = 不用代理）。
NO_PROXY_OPTS = {"proxies": {"http": "", "https": ""}}


class RetryLater(RuntimeError):
    """「现在不行，但过一会儿就好」：网络断了 / 代理挂了 / 号池暂时全在冷却。

    和普通失败的区别：这种错**不怪账号**，不该 `mark_fail`（否则好好的号被标成
    「连续失败」歇 20 分钟），也不该触发 20~40 分钟的限流熔断 —— 代理抖一下
    就要停半小时，是比限流本身更糟的误伤。
    """


# 判「网络层失败」用的特征串（小写）。只看类型+文本，不做精确匹配：
# curl_cffi 0.7 把底层错误包成 RequestsError，异常类型名各版本不同，文本反而稳定。
NET_HINTS = ("failed to perform", "curl: (6)", "curl: (7)", "curl: (28)",
             "curl: (35)", "curl: (52)", "could not connect", "connection refused",
             "connection reset", "connection aborted", "connectionerror",
             "timed out", "timeout was reached", "name resolution",
             "could not resolve", "eof occurred", "ssl", "proxy",
             "jsondecodeerror")


def is_net_error(e):
    """这个异常是不是「网络/环境」层面的，而不是账号或风控层面的。"""
    s = ("%s %s" % (type(e).__name__, e)).lower()
    return any(k in s for k in NET_HINTS)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg, also_print=True):
    """写一行日志（同时追加到 watch.log，方便事后查昨晚到底跑了什么）。"""
    line = "[%s] %s" % (now_str(), msg)
    if also_print:
        try:
            print(line, flush=True)
        except Exception:
            pass
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        # 日志文件超过 4MB 就轮转一份，避免无限长
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 4 * 1024 * 1024:
            old = LOG_FILE + ".1"
            try:
                if os.path.exists(old):
                    os.remove(old)
                os.replace(LOG_FILE, old)
            except OSError:
                pass
        with open(LOG_FILE, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, data):
    """先写临时文件再换名：中途断电不会留下半个 JSON（下一轮就读不出来了）。

    换名会撞 WinError 32/5：面板、扫描、别的追更实例可能同时持有同一个文件。
    这种情况下 tmp 是完整的，重试几次就能成功，不该让整轮循环崩掉。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    for i in range(6):
        try:
            os.replace(tmp, path)
            return
        except (PermissionError, OSError):
            if i == 5:
                raise
            time.sleep(0.15 * (i + 1))


def clean_part(s, limit=None):
    """清洗成能进 Windows 文件名的一段。"""
    s = BAD_CHARS.sub("_", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    # 结尾的点和空格 Windows 会静默吃掉，导致「明明下了却找不到」
    s = s.rstrip(" .")
    if limit and len(s) > limit:
        s = s[:limit].rstrip(" .")
    return s


def norm_desc(s):
    """判重用的描述规范化：去掉所有空白和下划线（不同来源的同一句话常常差一个空格）。"""
    return re.sub(r"[\s_]+", "", str(s or "")).lower()


# 描述里只是「装饰」的字符：表情、符号、变体选择符、零宽连接符、©®™。
# 抖音上改文案加/删一个表情太常见了，而**话题 # 和提及 @ 不在此列**（那是内容的一部分）。
DECO_RE = re.compile(
    "[\U0001F000-\U0001FAFF"      # 表情 / 交通 / 补充符号
    "\u2600-\u27BF"               # 杂项符号、装饰（☀✂✅ 之类）
    "\u2B00-\u2BFF"               # 箭头、星形
    "\uFE00-\uFE0F"               # 变体选择符（️）
    "\u200D\u20E3"                # 零宽连接符、键帽
    "\u00A9\u00AE\u2122"          # © ® ™
    "]+"
)


def deco_strip(s):
    """剥掉描述里的装饰性表情/符号，只留文字与话题标记。判重用，不用于文件名。"""
    return DECO_RE.sub("", str(s or ""))


def _char_width(ch):
    """DouK 算长度时把中日韩字符当 2 个字宽（跟它 beautify_string 一致）。"""
    return 2 if "CJK" in unicodedata.name(ch, "") else 1


def _trunc_width(s, width):
    n = 0
    out = []
    for ch in s:
        n += _char_width(ch)
        if n > width:
            break
        out.append(ch)
    return "".join(out)


def beautify_desc(s, length=64):
    """复刻 DouK 5.8 的 `beautify_string`：超长取**头 + 尾**、中间用 `...` 省略。

    为什么要一模一样：库里的老文件名全是这个格式（`前面一段...后面一段`）。我们
    自己下的时候如果只是「按字数硬切」，同一件作品就会得到**两个不同的名字** ——
    2026-11-08 就因此多下出 10 条重复（内容一模一样，只是名字不同）。
    """
    s = s or ""
    w = 0
    for ch in s:
        w += _char_width(ch)
        if w > length:
            break
    else:
        return s                       # 没超长，原样
    half = length // 2
    return "%s...%s" % (_trunc_width(s, half), _trunc_width(s[::-1], half)[::-1])


def name_desc(desc, desc_len=64):
    """作品名里的「描述段」：DouK 口径（超长头尾省略 + 洗掉非法字符）。

    ⚠️ 2026-11-09 起**不再用它拼文件名**（DouK 是拿「日期-昵称-描述」整串去清洗
    和截断的，见 douk_detail_name）。这里保留是因为判重 key 还按这个口径算，
    库里大量老文件名也是这个形状。
    """
    return clean_part(beautify_desc(desc or "", desc_len))


# ---------------------------------------------------------------------------
# DouK 口径的文件名（2026-11-09）
#
# 为什么要一字不差地复刻：DouK 判断「这条下过没」除了看 download_data 表，
# 还会看**目标文件在不在**。追更下的名字只要跟 DouK 生成的不一样，DouK 就会
# 当成新作品再下一份 —— 用户就是这么看到重复的。
#
# 规则全部来自 DouK 5.8 的 pyc（已反汇编核对，并用它自己的代码跑过对照）：
#   generate_detail_name (lib/src/downloader/download.pyc)
#     = beautify_string(filter_name(split.join([data[f] for f in name_format]), data['id']),
#                       length=name_length)
#   filter_name (lib/src/tools/cleaner.pyc)
#     = ':' → '.'  →  删非法字符  →  删表情  →  连续空格压成一个  →  strip()  →  strip('.')
#       清完为空就用作品 ID
#   beautify_string (lib/src/tools/truncate.pyc)：按显示宽度（CJK 算 2）超过 length
#     才截断，取「头 length//2 + '...' + 尾 length//2」
# 配置默认值（lib/src/config/settings.pyc）：name_length=128、split='-'、
# name_format='create_time type nickname desc'，用户实际配的是
# 'create_time nickname desc'（没有 type 段）。
# ---------------------------------------------------------------------------
DOUK_NAME_LENGTH = 128
DOUK_NAME_SPLIT = "-"

# 非法字符是**直接删掉**，不是换成下划线（DouK 的 rule 里每个值都是空串）。
# 控制字符整段一起删（含 \t \n \r）—— 它们要是留到后面被 \s+ 压成空格，
# 名字就和 DouK 不一样了（DouK 是删掉，`a\tb` 得到 `ab` 而不是 `a b`）。
_ILLEGAL_NAME_CHARS = re.compile(r'[/\\|<>"?*\x00-\x1f\x7f]')

try:                      # DouK 用的是 emoji 库的 replace_emoji(默认替换成空串)
    import emoji as _emoji_lib
except Exception:         # 3.13 那边没装；退回自己的正则，覆盖 99% 以上
    _emoji_lib = None


def drop_emoji(text):
    """删掉表情符号。优先用 emoji 库（和 DouK 同一个库，结果一致）。"""
    s = str(text or "")
    if _emoji_lib is not None:
        try:
            return _emoji_lib.replace_emoji(s, "")
        except Exception:
            pass
    return DECO_RE.sub("", s)


def douk_filter_name(text, default=""):
    """复刻 DouK 的 Cleaner.filter_name。顺序不能改，改一步名字就不一样。"""
    t = str(text or "").replace(":", ".")        # ① 冒号换成点
    t = _ILLEGAL_NAME_CHARS.sub("", t)           # ② 非法字符直接删
    t = drop_emoji(t)                            # ③ 表情直接删
    t = re.sub(r"\s+", " ", t).strip()           # ④ 空白归一 + 去首尾空白
    t = t.strip(".")                             # ⑤ 再去首尾的点
    return t or str(default or "")               # ⑥ 空了才拿作品 ID 兜底


def douk_detail_name(date, nickname, desc, aid="", length=DOUK_NAME_LENGTH):
    """按 DouK 的规则拼出作品文件名（不含扩展名、不含序号）。

    注意是**整串** `日期-昵称-描述` 一起清洗、一起按 128 宽截断，
    不是分段处理 —— 分段和整串截断在长描述上会得到完全不同的名字。
    """
    joined = DOUK_NAME_SPLIT.join([str(date or ""), str(nickname or ""), str(desc or "")])
    return beautify_desc(douk_filter_name(joined, aid), length)



def key_of_desc(desc, desc_len=64):
    """接口描述 → 判重 key（跟文件名里那段同名同形，能精确对上）。"""
    return norm_desc(name_desc(desc, desc_len))


def work_key(desc, desc_len=64):
    """判重 key 的**老口径**：按字数截断，再规范化。

    保留它是因为库里还有我们自己早期下载用这个口径命名的文件。新代码一律用
    `key_of_desc()`；`is_known()` 两个都试。
    """
    return norm_desc(clean_part(desc or "", desc_len))


# 老版本 DouK 会把过长的描述用省略号吃掉中段，例如：
#   库里：`我真的特别爱你～#我真的特别爱你 #... #是你喜欢的身材吗 #小肚小肚我在`
#   接口：`我真的特别爱你～#我真的特别爱你 #肉腿微胖 #是你喜欢的身材吗 #小肚小肚我在`
# 两边 work_key 永远对不上 → 全量扫一遍就重复下载（库里有 2.9 万条这种）。
# 所以判重不能只比全等，得把省略号当通配：前缀、后缀各自对上就算同一条。
ELLIPSE_RE = re.compile(r"\.{2,}|…")


def ellipse_split(key):
    """把已规范化的描述按**第一个**省略号拆成 (前缀, 后缀)。没有省略号返回 None。"""
    m = ELLIPSE_RE.search(key or "")
    if not m:
        return None
    return key[:m.start()], key[m.end():]


def build_date_index(local):
    """{(date, key)} → {date: [key, ...]}。模糊判重只在同一天内做。"""
    out = {}
    for date, key in local:
        out.setdefault(date, []).append(key)
    return out


def _ell_hit(full, pre, suf):
    """pre/suf 是否分别贴住 full 的两端。太短的一律不认，免得短后缀乱认亲。"""
    if len(pre) + len(suf) < 6:
        return False
    if pre and not full.startswith(pre):
        return False
    if suf and not full.endswith(suf):
        return False
    return True


def _pre_hit(full, pre):
    """只比开头：作者**改过描述尾巴**时，后缀永远对不上，只能靠开头认人。

    门槛故意定得高（规范化后 ≥12 字）：同一天、同一个作者的另外一件作品，
    开头 12 个字一模一样的概率极低；而 `#卡点舞` 这类短前缀根本进不来。
    2026-11-08 那 10 条就是这种情况（DouK 当年的名字结尾是 `#卡点`，
    现在接口给的是 `#卡点舞`，后缀对不上 → 每条都被当成新作品又下了一遍）。
    """
    return len(pre) >= 12 and full.startswith(pre)


def is_known(local, date_idx, date, desc, desc_len=64):
    """这条作品本地到底有没有（精确 key + 省略号模糊）。

    模糊匹配必须限定在**同一天**：`#卡点` 这种短前缀放到全库去比，会把别人的作品
    错认成自己 —— 误判成「已有」就会永远漏下这一条，比重复下载严重得多。

    2026-11-08 补两处（都会导致**重复下载**，不是小事）：
      1. 精确比对除了老口径，还要试 DouK 口径（头尾省略）—— 我们自己的命名现在
         跟 DouK 一样了，这样绝大多数作品第一次就能精确命中；
      2. 模糊比对的后缀要用**未截断的完整描述**去比。按 64 字硬切的话，长描述
         结尾那段被切掉了，`full.endswith(库里的后缀)` 永远不成立 → 判成新作品
         又下一遍（2026-11-08 那 10 条就这么重复的）。
    """
    key = work_key(desc, desc_len)
    if (date, key) in local:
        return True
    bkey = key_of_desc(desc, desc_len)
    if bkey != key and (date, bkey) in local:
        return True
    same = date_idx.get(date) or ()
    if not same:
        return False
    full = norm_desc(desc or "")          # 不截断：后缀必须贴住真正的结尾
    # 方向一：库里那条被省略了，接口给的是完整描述（绝大多数情况走这条）
    for k in same:
        sp = ellipse_split(k)
        if sp and (_ell_hit(full, sp[0], sp[1]) or _pre_hit(full, sp[0])):
            return True
    # 方向二：接口给的被省略了，库里留着完整的那条
    sp = ellipse_split(bkey)
    if sp:
        for k in same:
            if _ell_hit(k, sp[0], sp[1]):
                return True
    # 方向三：两边只差装饰性表情（2026-11-09 的「🥲#卡点舞」）
    #
    #   DouK 11-07 下的文件名是 `-糊涂思^-#卡点舞.mp4`，接口现在给的描述是
    #   `🥲#卡点舞` → 名字不同 → 判成新作品，又下了一份 1.39MB 的。
    #   抖音上给旧作品改文案、加个表情太常见，文件名对不上不代表作品不一样。
    #   剥掉表情后完全相同才算同一条；剥完太短的（<4 字）不参与，
    #   否则同一天两三条 `#卡点舞` / `#变装` 会被认成一条，那是漏下，比重复更糟。
    bare = deco_strip(bkey)
    if len(bare) >= 4 and bare != bkey:
        for k in same:
            if deco_strip(k) == bare:
                return True
    return False


# ---------------------------------------------------------------------------
# 名单与状态
# ---------------------------------------------------------------------------

def load_watch():
    d = _read_json(WATCH_FILE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("authors", [])
    # 开机自动接上：用户晚上关电脑，早上开机打开界面就该自己补漏，不该再点一次
    d.setdefault("auto_start", True)
    raw = d.get("settings") or {}
    cfg = dict(DEFAULT_CFG)
    cfg.update(raw)
    # 一次性迁移：老配置只有「热/冷」两档（hot_days=7 意味着 7 天内发过的都 15 分钟查
    # 一次，200 位作者就是每天一万九千次请求，太猛）。补上温/静两档后，把热档收窄到 2 天。
    if raw and "warm_days" not in raw:
        cfg["hot_days"] = DEFAULT_CFG["hot_days"]
    d["settings"] = cfg
    return d


def save_watch(d):
    _write_json(WATCH_FILE, d)


def load_state():
    d = _read_json(STATE_FILE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("authors", {})
    # 风控相关的运行时字段，缺就补默认值（老的状态文件也能直接用）
    d.setdefault("req_day", time.strftime("%Y-%m-%d"))
    d.setdefault("req_today", 0)
    d.setdefault("consec_fail", 0)
    d.setdefault("cool_until", 0)
    d.setdefault("cool_level", 0)
    # 一次性迁移（2026-11-09）：旧算法把首次基线取成「本地最后作品那天 23:59:59」，
    # 于是当天更早发布、但当时没下到的作品全被划进「老作品」，永远补不下来。
    # 真实作品推进的 last_ts 不可能正好落在 23:59:59，按这个特征回退到当天 00:00，
    # 让这批作者把当天重新核对一遍（已下过的由第 3 层判重挡掉，不会重复下）。
    if not d.get("base_dayfix"):
        fixed = 0
        for _st in (d.get("authors") or {}).values():
            try:
                _lt = int(_st.get("last_ts") or 0)
            except (TypeError, ValueError):
                continue
            if not _lt:
                continue
            _t = time.localtime(_lt)
            if (_t.tm_hour, _t.tm_min, _t.tm_sec) == (23, 59, 59):
                _st["last_ts"] = int(time.mktime(
                    (_t.tm_year, _t.tm_mon, _t.tm_mday, 0, 0, 0, 0, 0, -1)))
                fixed += 1
        d["base_dayfix"] = True
        if fixed:
            print("[watch] 基线修正：%d 位作者的 last_ts 从「当天 23:59:59」回退到「当天 00:00」" % fixed)
        save_state(d)
    return d


def save_state(d):
    _write_json(STATE_FILE, d)


# ---------------------------------------------------------------------------
# 逐条下载明细（watch_downloads.jsonl）
#
# 主界面「累计追更下载 N 条」那张卡片要能点开看明细，观感照「新下载」。
# watch_state 里每个作者只有一个计数，撑不起这个视图，所以单独记流水。
# 一行一条作品（图文作品的多张图合成一条，rels 里放全部相对路径）。
# ---------------------------------------------------------------------------

_DL_LOCK = __import__("threading").Lock()


def record_download(uid, nickname, date, desc, rels, size, ts=None, backfill=False):
    """追加一条下载明细。rels 是相对 SCAN_ROOT 的路径（和 download_log.db 同一口径）。"""
    rels = [r for r in (rels or []) if r]
    if not rels:
        return False
    rec = {
        "ts": int(ts or time.time()),
        "uid": str(uid or ""),
        "nickname": (nickname or "").strip(),
        "date": (date or "").strip(),
        "desc": (desc or "").strip(),
        "rels": rels,
        "size": int(size or 0),
    }
    if backfill:
        rec["backfill"] = True
    try:
        with _DL_LOCK:
            os.makedirs(os.path.dirname(DOWNLOADS_FILE), exist_ok=True)
            with open(DOWNLOADS_FILE, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except OSError as e:      # 记流水失败绝不能影响下载本身
        print("[watch] 下载明细写入失败：%s" % e)
        return False


_DL_CACHE = {"key": None, "rows": []}


def load_downloads():
    """读回全部下载明细，按落盘时间从新到旧；坏行直接跳过（别让一行坏了整份读不出）。

    按 (mtime, size) 缓存：这个函数会被 /api/watch/status 5 秒一次调到，
    明细会越攒越多，每次都重解析没必要。文件一变（追加）就自动失效。
    """
    try:
        st = os.stat(DOWNLOADS_FILE)
        key = (st.st_mtime, st.st_size)
    except OSError:
        return []
    if _DL_CACHE["key"] == key:
        return _DL_CACHE["rows"]

    out = []
    seen = set()          # rel -> 已归属，回填和实时记录可能重复，按文件去重
    with open(DOWNLOADS_FILE, "r", encoding="utf-8", errors="replace") as fp:
        for ln in fp:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(r, dict):
                continue
            rels = [x for x in (r.get("rels") or []) if isinstance(x, str) and x]
            rels = [x for x in rels if x not in seen]
            if not rels:
                continue
            seen.update(rels)
            r["rels"] = rels
            r["files"] = len(rels)
            out.append(r)
    out.sort(key=lambda r: -(int(r.get("ts") or 0)))
    _DL_CACHE["key"] = key
    _DL_CACHE["rows"] = out
    return out


def download_count():
    """明细里的作品条数（实时记录 + 回填的合计）。"""
    return len(load_downloads())


# ---------------------------------------------------------------------------
# 本地库索引：判断「这条作品是不是早就有了」
# ---------------------------------------------------------------------------

def strip_nick_prefix(desc, nick):
    """把混进描述里的作者昵称剥掉。

    文件名是 `日期-昵称-描述.ext`，昵称自己带 `-` 的时候（比如 `-糊涂思^`）
    就变成 `2026-11-07--糊涂思^-#卡点舞.mp4`，按 `-` 切分会把昵称的一部分
    切进描述里 —— 索引里那条记成 `糊涂思^-#卡点舞`，而接口给的是 `#卡点舞`，
    两边永远对不上 → 同一件作品被判成新作品，又下一份。
    2026-11-09 的「🥲#卡点舞」重复就是这么来的（emoji 只是表层，错位才是根）。

    **只在剥完紧跟一个分隔符时才认定是错位**：`糊涂思^-#卡点舞` → `#卡点舞`（剥）。
    而 `冬日雪#宅男` 这种「描述本身就以昵称开头」的，剥完直接是 `#宅男`、没有
    分隔符 → 不剥，否则会把本来能对上的作品判成新作品（漏判比重复更糟）。
    """
    d = str(desc or "")
    n = str(nick or "").strip()
    if not d or not n:
        return d
    for cand in (n, n.lstrip("-—_–")):
        cand = cand.strip()
        if len(cand) < 2:
            continue
        if d.startswith(cand):
            rest = d[len(cand):]
            # 只认横线类分隔符。空格不行：`冬日雪 真好看`（描述本身以昵称开头、
            # 后面接空格）会被误剥成 `真好看`，接口给的是完整句 → 反而漏判。
            if rest[:1] in ("-", "—", "–"):
                return rest.lstrip("-—_– ")
    return d


def load_library_index():
    """返回 {uid: {(date, norm_desc)}, {uid: folder}}。

    直接读 data.json（scanner 的产物）。读不到就退化成空索引 —— 宁可多查一次
    文件系统，也不要因为一个 JSON 坏了就整个停摆。
    """
    idx, folders = {}, {}
    d = _read_json(paths.DATA_JSON, {})
    for uid, a in (d.get("accounts") or {}).items():
        uid = str(uid)
        folders[uid] = a.get("folder") or ""
        nick = a.get("nickname") or ""
        s = set()
        for w in (a.get("works") or []):
            date = str(w.get("date") or "")
            desc = w.get("desc")
            if desc is None:
                # scanner 有些老记录把 desc 存在文件名里，用 files 兜底
                rel = ((w.get("files") or [{}])[0] or {}).get("rel") or ""
                desc = parse_desc_from_name(rel.split("/")[-1])
            s.add((date, work_key(strip_nick_prefix(desc, nick))))
        idx[uid] = s
    return idx, folders


def douk_downloaded_ids():
    """DouK 下载记录里的全部 aweme_id（只读，读不到就返回空集）。

    用途：本地没有、但 DouK 记录里有 → 说明**用户自己删掉了**，补下载工具不能
    把它当「历史漏抓」再补回来。2026-11-08 就因为这个把 10 条已被删的老作品
    又下了一遍（用户删作品是为了不要它们，不是等它回来）。
    """
    ids = set()
    try:
        import sqlite3
        if not os.path.exists(paths.DOWNLOADER_DB):
            return ids
        con = sqlite3.connect("file:%s?mode=ro" % paths.DOWNLOADER_DB, uri=True, timeout=10)
        try:
            for (aid,) in con.execute("select * from download_data"):
                ids.add(str(aid))
        finally:
            con.close()
    except Exception as e:
        log("· 读 DouK 下载记录失败（不影响下载，只是判重少一层）：%s" % e)
    return ids


_DOUK_IDS_CACHE = {"ts": 0.0, "ids": set()}


def douk_id_set(ttl=60):
    """DouK 下载记录（aweme_id 集合），60 秒内复用。

    追更每轮都会看一遍这个表：DouK 是按 **ID** 判重的，不受文件名影响，
    比我们按描述比名字可靠得多。读失败返回空集，此时退回纯文件名判重。
    """
    now = time.time()
    if _DOUK_IDS_CACHE["ids"] and now - _DOUK_IDS_CACHE["ts"] <= ttl:
        return _DOUK_IDS_CACHE["ids"]
    ids = douk_downloaded_ids()
    if ids:
        _DOUK_IDS_CACHE["ts"] = now
        _DOUK_IDS_CACHE["ids"] = ids
    return ids


def douk_mark_downloaded(aid):
    """把作品 ID 记进 DouK 的下载记录表（download_data.ID 是 TEXT 主键）。

    为什么必须做：DouK 判重是两条 —— ① ID 在 download_data 里；② 目标文件存在。
    追更下的文件天然不会进这张表，于是只剩「文件名」这一条依据；而文件名在
    **作者改过昵称或改过文案**之后必然对不上（库里 08蓉 的老文件还叫
    `08蓉（上学版）`，现在昵称是 `08蓉`），DouK 就会把同一件作品再下一份。
    写成 ID 以后，DouK 认的是作品本身，跟名字无关。

    失败不抛错：DouK 可能正在跑、库可能被独占，这时退回文件名校验即可，
    不能因为记一笔账就让整条下载流程失败。
    """
    aid = str(aid or "").strip()
    if not aid:
        return False
    try:
        import sqlite3
        if not os.path.exists(paths.DOWNLOADER_DB):
            return False
        con = sqlite3.connect(paths.DOWNLOADER_DB, timeout=10)
        try:
            con.execute("INSERT OR IGNORE INTO download_data(ID) VALUES (?)", (aid,))
            con.commit()
        finally:
            con.close()
        # 让这一轮的判重缓存立刻看到新 ID
        _DOUK_IDS_CACHE["ids"].add(aid)
        return True
    except Exception as e:
        log("· 写入 DouK 下载记录失败（不影响下载，只是 DouK 那边少一条判重依据）：%s"
            % str(e)[:100])
        return False


def parse_desc_from_name(name):
    """从 `日期-昵称-描述[_序号].ext` 里抠出描述（老记录兜底用）。"""
    m = re.match(r"^\d{4}-\d{2}-\d{2}-(.*?)-(.*?)(?:_(\d+))?\.[A-Za-z0-9]+$", name)
    return m.group(2) if m else name


# ---------------------------------------------------------------------------
# 账号池：一个号被限流就换下一个
#
# 为什么需要：抖音对单个登录态的调用频率是有软上限的。以前只有一条链路时，
# 一被限流就只能整体停摆等冷却。实际用起来大家都有几个号 —— 一个号查不动了，
# 换另一个继续就行，既不耽误时间，也不会在同一个号上反复试探（那才最容易被盯上）。
#
# 规则：
#   1. 每个号自己记连续失败次数；连续 2 次不成 → 这个号先歇 5 分钟（再犯翻倍）。
#   2. 查询一失败**立刻换号**再试一次，不在同一个号上重试第三、第四次。
#   3. 换号时挑「今天用得最少」的那个，几个号自然均摊。
# ---------------------------------------------------------------------------

def _cookie_dict(v):
    """把 cookie 统一成 dict：支持 dict / JSON 串 / 'a=1; b=2' 这种原文。"""
    if isinstance(v, dict):
        return {str(k): str(x) for k, x in v.items() if x not in (None, "")}
    s = str(v or "").strip()
    if not s:
        return {}
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return {str(k): str(x) for k, x in d.items() if x not in (None, "")}
        except Exception:
            pass
    out = {}
    for part in re.split(r"[;\n]", s):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, val = part.split("=", 1)
        if k.strip():
            out[k.strip()] = val.strip()
    return out


def _new_account(label, cookie, browser_info=None, aid=None):
    ck = _cookie_dict(cookie)
    aid = aid or "acc%d" % int(time.time() * 1000 % 100000000)
    return {
        "id": aid, "label": str(label or "").strip() or "账号",
        "cookie": ck, "browser_info": browser_info or {},
        "added_at": now_str(), "enabled": True,
        "ok": 0, "fail": 0, "cool_until": 0, "ban_level": 0,
        "req_today": 0, "req_day": "", "last_error": "",
    }


def load_pool():
    """读账号池。第一次用的时候把下载器当前登录态导进来当 1 号（开箱即用）。

    注意：导入的默认号**必须落盘**，id 固定成 "default"。
    早期版本每次调用现造一个时间戳 id，结果面板上「停用 / 移出」按 id 找不到它 ——
    因为两次调用生成的 id 根本不是同一个。
    """
    d = _read_json(ACCOUNTS_FILE, {})
    if not isinstance(d, dict):
        d = {}
    accs = d.get("accounts")
    if not isinstance(accs, list):
        accs = []
    accs = [a for a in accs if isinstance(a, dict)]
    # imported 标记：用户把号全删干净后，别再自作聪明把默认号塞回去
    if not accs and not d.get("imported"):
        st = _read_json(paths.DOWNLOADER_SETTINGS, {})
        ck = _cookie_dict((st or {}).get("cookie") or {})
        if ck.get("sessionid"):
            accs = [_new_account("下载器账号（默认）", ck,
                                 (st or {}).get("browser_info") or {}, aid="default")]
            d["accounts"] = accs
            d["imported"] = True
            save_pool(d)
    d["accounts"] = accs
    return d


def save_pool(d):
    _write_json(ACCOUNTS_FILE, d)


def pool_summary():
    """给面板看：不返回完整 cookie，只给能判断状态的信息。"""
    d = load_pool()
    day = time.strftime("%Y-%m-%d")
    now = time.time()
    out = []
    for a in d.get("accounts") or []:
        ck = a.get("cookie") or {}
        sid = str(ck.get("sessionid") or ck.get("sessionid_ss") or "")
        out.append({
            "id": a.get("id") or "",
            "label": a.get("label") or "",
            "enabled": bool(a.get("enabled", True)),
            "logged_in": bool(sid),
            "sid_tail": sid[-6:] if sid else "",
            "ok": int(a.get("ok") or 0),
            "fail": int(a.get("fail") or 0),
            "cool_left": max(0, int(float(a.get("cool_until") or 0) - now)),
            "req_today": int(a.get("req_today") or 0) if a.get("req_day") == day else 0,
            "last_error": str(a.get("last_error") or "")[:60],
        })
    return out


def pool_add(label, cookie):
    ck = _cookie_dict(cookie)
    if not ck.get("sessionid") and not ck.get("sessionid_ss"):
        return {"ok": False, "error": "这段 cookie 里没有 sessionid，不像是登录态"}
    d = load_pool()
    for a in d["accounts"]:
        if (a.get("cookie") or {}).get("sessionid") == ck.get("sessionid"):
            return {"ok": False, "error": "这个号已经在池子里了"}
    a = _new_account(label, ck)
    d["accounts"].append(a)
    save_pool(d)
    return {"ok": True, "id": a["id"], "accounts": pool_summary()}


def pool_sync_downloader():
    """把下载器 settings.json 里当前的登录态同步进账号池。

    用途：你在 DouK 里换了个号，追更这边不该还拿着旧 cookie。
    """
    st = _read_json(paths.DOWNLOADER_SETTINGS, {})
    ck = _cookie_dict((st or {}).get("cookie") or {})
    if not ck.get("sessionid"):
        return {"ok": False, "error": "下载器 settings.json 里没有登录态（sessionid 为空）"}
    bi = (st or {}).get("browser_info") or {}
    d = load_pool()
    for a in d["accounts"]:
        if a.get("id") == "default":
            a["cookie"] = ck
            a["browser_info"] = bi or a.get("browser_info") or {}
            save_pool(d)
            return {"ok": True, "accounts": pool_summary()}
    d["accounts"].append(_new_account("下载器账号（默认）", ck, bi, aid="default"))
    d["imported"] = True
    save_pool(d)
    return {"ok": True, "accounts": pool_summary()}


def pool_remove(aid):
    d = load_pool()
    before = len(d["accounts"])
    d["accounts"] = [a for a in d["accounts"] if str(a.get("id")) != str(aid)]
    if len(d["accounts"]) == before:
        return {"ok": False, "error": "没找到这个号"}
    save_pool(d)
    return {"ok": True, "accounts": pool_summary()}


def pool_toggle(aid, enabled):
    d = load_pool()
    for a in d["accounts"]:
        if str(a.get("id")) == str(aid):
            a["enabled"] = bool(enabled)
            save_pool(d)
            return {"ok": True, "accounts": pool_summary()}
    return {"ok": False, "error": "没找到这个号"}


class AccountPool(object):
    """当前用哪个号、失败怎么换、换到谁。"""

    def __init__(self):
        self.d = load_pool()
        self.i = 0
        self.reset_day()

    @property
    def accounts(self):
        return self.d.get("accounts") or []

    def reset_day(self):
        day = time.strftime("%Y-%m-%d")
        changed = False
        for a in self.accounts:
            if a.get("req_day") != day:
                a["req_day"] = day
                a["req_today"] = 0
                changed = True
        return changed

    def usable(self, now=None):
        now = now or time.time()
        out = []
        for a in self.accounts:
            ck = a.get("cookie") or {}
            if not a.get("enabled", True):
                continue
            if not (ck.get("sessionid") or ck.get("sessionid_ss")):
                continue
            if float(a.get("cool_until") or 0) > now:
                continue
            out.append(a)
        return out

    def current(self, now=None):
        now = now or time.time()
        us = self.usable(now)
        if not us:
            return None
        cur = self.accounts[self.i] if 0 <= self.i < len(self.accounts) else None
        if cur is not None and any(cur is a for a in us):
            return cur
        best = min(us, key=lambda x: int(x.get("req_today") or 0))
        self.i = self.accounts.index(best)
        return best

    def rotate(self, why="", now=None):
        """换到另一个还能用的号。返回 True 表示换了。"""
        now = now or time.time()
        us = self.usable(now)
        if len(us) < 2:
            return False
        cur = self.current(now)
        cands = [a for a in us if a is not cur]
        if not cands:
            return False
        nxt = min(cands, key=lambda x: int(x.get("req_today") or 0))
        self.i = self.accounts.index(nxt)
        log("⇄ 换号 → %s（%s）" % (nxt.get("label") or nxt.get("id"), why or "上一个号失败"))
        return True

    def mark_ok(self):
        a = self.current()
        if not a:
            return
        a["ok"] = int(a.get("ok") or 0) + 1
        a["fail"] = 0
        a["ban_level"] = 0

    def mark_fail(self, why="", ban=None, cfg=None):
        """记一次失败。同一个号连续 2 次不成 → 这个号歇一会儿（别的号继续）。"""
        a = self.current()
        if not a:
            return
        a["fail"] = int(a.get("fail") or 0) + 1
        a["last_error"] = str(why or "")[:120]
        if a["fail"] >= 2:
            base = float(ban or (cfg or {}).get("account_ban") or 300)
            lvl = min(int(a.get("ban_level") or 0), 3)
            a["cool_until"] = time.time() + base * (2 ** lvl)
            a["ban_level"] = lvl + 1
            a["fail"] = 0
            log("⚠ 号「%s」连续失败，先歇 %.0f 分钟，换别的号继续"
                % (a.get("label") or a.get("id"), base * (2 ** lvl) / 60))
        save_pool(self.d)

    def snapshot(self):
        day = time.strftime("%Y-%m-%d")
        out = []
        for a in self.accounts:
            out.append({
                "id": a.get("id"), "label": a.get("label") or "",
                "ok": int(a.get("ok") or 0), "fail": int(a.get("fail") or 0),
                "cool_left": max(0, int(float(a.get("cool_until") or 0) - time.time())),
                "req_today": int(a.get("req_today") or 0) if a.get("req_day") == day else 0,
            })
        return out


# ---------------------------------------------------------------------------
# 抖音接口
# ---------------------------------------------------------------------------

class Douyin(object):
    """包一层签名请求，避免每个调用点都重复 build_query / a_bogus。"""

    def __init__(self):
        import fetch_favorites as ff
        self.ff = ff
        ff.init_env()
        from curl_cffi import requests as cr
        try:
            from src.encrypt.aBogus import ABogus
        except ImportError:
            from encrypt.aBogus import ABogus
        self.cr = cr
        self.pool = AccountPool()
        if not self.pool.accounts:
            raise RuntimeError("账号池是空的：先在面板里加一个抖音号的 cookie")
        cur = self.pool.current()
        if cur is None:
            # 池子里有号但全在冷却 —— 等它们缓过来就行，不是故障
            raise RetryLater("账号池里没有可用的号（都没登录或全在冷却）")
        # browser_info 优先用这个号自己的，没有就退回下载器那份
        st, _ck = ff.load_ctx()
        self.bi = (cur.get("browser_info")
                   or (st or {}).get("browser_info") or {})
        self.ab = ABogus(user_agent=ff.UA)
        # 真实发出去的 API 请求数。含重试 —— 额度必须按"实际打了几枪"算，
        # 只按"查了几位作者"算会低估一倍以上（失败时一次查询最多打 2 枪）。
        self.reqs = 0
        self.switches = 0

    def current_cookie(self):
        a = self.pool.current()
        return (a or {}).get("cookie") or {}

    def get(self, url, extra, referer="https://www.douyin.com/"):
        acct = self.pool.current()
        if acct is None:
            # 号都在冷却 ≠ 号坏了。当成网络类「等会儿再来」，不记账、不熔断
            raise RetryLater("账号池里没有可用的号（都在冷却或没登录）")
        q = self.ff.build_query(self.bi)
        q.update(extra)
        q["a_bogus"] = self.ab.get_value(
            self.ff.urlencode(q), body="",
            content_type="application/x-www-form-urlencoded")
        self.reqs += 1
        acct["req_today"] = int(acct.get("req_today") or 0) + 1
        return self.cr.get(url, params=q,
                           headers=self.ff.make_headers(self.current_cookie(), referer),
                           impersonate="chrome146", timeout=25,
                           **NO_PROXY_OPTS)

    def author_works(self, sec_uid, pages=1, retry=2):
        """拉作者主页作品列表（新→旧）。返回 aweme_list。

        失败时**先换号再重试**：同一个号上连着打三四枪既慢又最容易被盯上，
        换个号往往一次就过。只有池子里没有别的号可用时才原地等。
        """
        out, cursor, seen = [], "0", set()
        for _ in range(max(1, pages)):
            last = ""
            net = False
            data = None
            for attempt in range(1, max(1, retry) + 1):
                net = False
                try:
                    r = self.get(
                        "https://www.douyin.com/aweme/v1/web/aweme/post/",
                        {"sec_user_id": sec_uid, "count": "20", "max_cursor": cursor,
                         "locate_query": "false", "publish_video_strategy_type": "2"},
                        referer="https://www.douyin.com/user/" + sec_uid)
                    if r.status_code != 200:
                        last = "HTTP %s" % r.status_code
                        self.pool.mark_fail(last)
                    else:
                        d = r.json()
                        if d.get("status_code") in (0, None):
                            data = d
                            self.pool.mark_ok()
                            break
                        last = "status=%s %s" % (d.get("status_code"), d.get("status_msg"))
                        self.pool.mark_fail(last)
                except Exception as e:
                    last = "%s: %s" % (type(e).__name__, e)
                    net = is_net_error(e)
                    # 网络层的失败不记在账号头上：换个号一样连不上，记了只会
                    # 把几个好号挨个打成「连续失败」，越跑越像被封
                    if not net:
                        self.pool.mark_fail(last)
                if attempt < max(1, retry):
                    if net:
                        time.sleep(1.5)          # 网络问题换号没意义，原地短等
                    elif self.pool.rotate(last):
                        self.switches += 1
                        time.sleep(0.5)
                    else:
                        time.sleep(attempt * 1.5)
            if not data:
                if out:
                    break
                if net:
                    raise RetryLater(last or "网络不通")
                raise RuntimeError(last or "查询失败")
            al = data.get("aweme_list") or []
            for a in al:
                aid = str(a.get("aweme_id") or "")
                if aid and aid not in seen:
                    seen.add(aid)
                    out.append(a)
            if not data.get("has_more") or not al:
                break
            nxt = str(data.get("max_cursor") or "")
            if not nxt or nxt == cursor:
                break
            cursor = nxt
            time.sleep(1.0)          # 翻页也留点间隔，别一秒打两枪
        return out


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------

def pick_url(url_list):
    """url_list 里的地址按清晰度升序排，**取最后一个**才是高画质那份。

    抖音图文作品的 url_list 通常长这样：[webp 预览, webp 预览(mirror), jpeg 原图]
    —— 2026-11-08 实测同一张图：前两个 9.2 KB（1320×258 的糊图），最后一个 19.8 KB
    的 jpeg 才是真正的图。DouK 5.8 也是取 [-1]（`IMAGE_INDEX = -1` / `VIDEO_INDEX = -1`），
    所以这里倒序遍历，后面的地址优先。失败重试时还会依次试剩下的镜像。
    """
    return [u for u in reversed(url_list or [])
            if isinstance(u, str) and u.startswith("http")]


def pick_best_video(video):
    """挑清晰度最高的那条流。

    抖音返回的 video.bit_rate 是按码率排的一串，play_addr 是「默认清晰度」。
    排序口径跟 DouK 5.8 一致：(长边, 帧率, 码率, 体积) 取最大 —— 单纯比体积会
    被「小尺寸高码率」骗过去。追更下的量不大，尽量拿好的那个。
    """
    best, best_score = None, None
    for br in (video.get("bit_rate") or []):
        addr = br.get("play_addr") or {}
        urls = pick_url(addr.get("url_list"))
        if not urls:
            continue
        score = (max(int(addr.get("height") or 0), int(addr.get("width") or 0)),
                 int(br.get("FPS") or 0), int(br.get("bit_rate") or 0),
                 int(addr.get("data_size") or 0))
        if best_score is None or score > best_score:
            best_score, best = score, urls
    if best:
        return best
    return pick_url((video.get("play_addr") or {}).get("url_list"))


def _ext_from_type(ct):
    ct = (ct or "").split(";")[0].strip().lower()
    return IMG_EXT_BY_TYPE.get(ct, "")


def is_anim_webp(head):
    """判断 webp 是不是「动图」：容器里有 ANIM 块（RIFF/WEBP + VP8X + ANIM）。

    DouK 5.8 不看这个，它只按 Content-Type 定后缀；所以 DouK 下的动图会存成
    .webp 一张 —— 在 Windows 照片/很多播放器里只显示第一帧，看起来就是「不动」。
    这里单独认出来，转成 mp4 才能真正播放（2026-11-08 用户要求）。
    """
    if not head or head[:4] != b"RIFF" or b"WEBP" not in head[:16]:
        return False
    return b"ANIM" in head[:128] or b"ANMF" in head[:128]


def is_anim(head):
    return bool(head) and (head[:4] == b"GIF8" or is_anim_webp(head))


def find_ffmpeg():
    """找 ffmpeg：先 PATH，再 DouK 自带的（DouK 的实况合成就是用它）。"""
    p = shutil.which("ffmpeg")
    if p:
        return p
    for cand in (
        os.path.join(paths.DOWNLOADER_DIR, "ffmpeg.exe"),
        os.path.join(paths.DOWNLOADER_DIR, "ffmpeg", "ffmpeg.exe"),
        os.path.join(paths.MEDIA_BASE, "ffmpeg.exe"),
    ):
        if os.path.isfile(cand):
            return cand
    return ""


def _ffmpeg_run(exe, args, timeout=180):
    try:
        r = subprocess.run([exe, "-y", "-loglevel", "error"] + args,
                           capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stderr or b"").decode("utf-8", "replace")
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e)


def anim_to_mp4(src, dst):
    """动图（动态 webp / GIF）转 mp4。成功返回文件字节数，失败返回 0。

    两条路：
      1. ffmpeg 直接读 —— GIF 可以，**动态 webp 不行**（ffmpeg 的 webp 解码器
         不支持多帧，实测 8.1 报 `image data not found`）；
      2. 动态 webp 退回 Pillow 拆帧，再以 image2pipe 喂给 ffmpeg 编码。
         帧间隔按每帧自带的 duration 折算成 30fps 的重复次数，播放速度才不走样。
    """
    exe = find_ffmpeg()
    if not exe:
        return 0
    enc = ["-movflags", "+faststart", "-pix_fmt", "yuv420p",
           # 动图常见奇数宽高，h264 要求偶数，不补就报错
           "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
           "-an", dst]
    # 本机 ffmpeg 是 LGPL 版，没有 libx264，但带 libopenh264（同为 h264，
    # 浏览器/播放器都认）；万一这版没有，再退回默认编码器（mpeg4，兼容性差些）
    encs = [["-c:v", "libopenh264", "-b:v", "4M"] + enc, enc]
    err = ""
    for extra in encs:
        rc, err = _ffmpeg_run(exe, ["-i", src] + extra)
        if rc == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 1024:
            return os.path.getsize(dst)
    n = _frames_to_mp4(exe, src, dst, encs)
    if n:
        return n
    log("  ! 动图转 mp4 失败：%s" % err.strip()[:140])
    return 0


def _frames_to_mp4(exe, src, dst, encs):
    """Pillow 拆帧 → ffmpeg 编码。没有 Pillow 或文件不是动图就返回 0。"""
    try:
        from PIL import Image
    except Exception:
        return 0
    try:
        im = Image.open(src)
        if not getattr(im, "is_animated", False) or im.n_frames < 2:
            return 0
        base_fps = 30
        plan = []                      # [(frame, 重复次数)]
        for i in range(im.n_frames):
            im.seek(i)
            dur = im.info.get("duration") or 0
            plan.append((im.convert("RGBA").copy(), max(1, int(round(dur / (1000.0 / base_fps))))))
            if sum(p[1] for p in plan) > base_fps * 60:      # 最长 60 秒，别把内存吃光
                break
        import io
        blobs = []
        for frame, times in plan:
            buf = io.BytesIO()
            frame.save(buf, "PNG")
            blobs.append((buf.getvalue(), times))
        for extra in encs:
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
            cmd = [exe, "-y", "-loglevel", "error", "-f", "image2pipe",
                   "-vcodec", "png", "-framerate", str(base_fps), "-i", "-"] + extra
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for data, times in blobs:
                for _ in range(times):
                    try:
                        proc.stdin.write(data)
                    except Exception:
                        break
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.wait(timeout=180)
            if proc.returncode == 0 and os.path.isfile(dst) and os.path.getsize(dst) > 1024:
                return os.path.getsize(dst)
    except Exception as e:
        log("  ! 动图拆帧异常：%s: %s" % (type(e).__name__, e))
    return 0


def save_item(cr, urls, base, referer, retry=3):
    """下一个「作品项」到 base + 真实后缀。返回 (字节数, 后缀, 错误)。

    做法对齐 DouK 的 `_extract_content`：**后缀由服务器回的 Content-Type 决定**。
    这么做的原因：
      * 抖音图片 URL 里没有可靠后缀，猜 URL 经常把 webp 存成 .jpeg；
      * 实况/动图项的 URL 有时是图片域名的样子，实际回的是 video/mp4；
      * 动图（GIF / 动态 webp）需要认出来单独处理，否则「存了但不会动」。
    """
    tmp = base + ".part"
    last = ""
    for attempt in range(1, retry + 1):
        for u in urls:
            try:
                r = cr.get(u, headers={"User-Agent": UA, "Referer": referer},
                           impersonate="chrome146", timeout=60, stream=True,
                           **NO_PROXY_OPTS)
                if r.status_code != 200:
                    last = "HTTP %s" % r.status_code
                    r.close()
                    continue
                ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                total = 0
                head = b""
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(262144):
                        if not chunk:
                            continue
                        if len(head) < 128:
                            head += chunk[:128 - len(head)]
                        f.write(chunk)
                        total += len(chunk)
                r.close()
                if total < 1024:
                    last = "只拿到 %d 字节" % total
                    continue
                ext = _ext_from_type(ctype)
                if not ext:
                    ext = VIDEO_EXT if ctype.startswith("video/") else ""
                if not ext:
                    ext = ".gif" if head[:4] == b"GIF8" else ".jpeg"
                if ext in (".gif", ".webp") and is_anim(head):
                    # 动图：转成 mp4 才真能播（Windows 照片、大部分播放器都不放动图 webp）
                    dst = base + VIDEO_EXT
                    if anim_to_mp4(tmp, dst):
                        try:
                            os.remove(tmp)
                        except OSError:
                            pass
                        return os.path.getsize(dst), VIDEO_EXT, ""
                    # 转不了就原样留着，至少内容没丢
                    log("  ! %s 是动图但没转成 mp4，按原格式保留" % os.path.basename(base))
                os.replace(tmp, base + ext)
                return total, ext, ""
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, str(e)[:80])
            finally:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
        time.sleep(attempt * 2)
    return 0, "", last


def existing_of(base, exts):
    """base 已经下过、且后缀在 exts 里的那个文件（没有返回空串）。"""
    for e in exts:
        p = base + e
        if os.path.exists(p) and os.path.getsize(p) > 1024:
            return p
    return ""


def aweme_kind(aweme):
    """这条作品是「视频」还是「图文」。判据和 download_aweme 里那一句完全一致：
    images / image_post_info.images 有内容就是图文（实况、动图也算图文）。"""
    aweme = aweme or {}
    if aweme.get("images") or ((aweme.get("image_post_info") or {}).get("images") or []):
        return "image"
    return "video"


def download_aweme(dy, aweme, author_dir, nickname, desc_len=None, out=None):
    """下一条作品到 author_dir。返回 (文件数, 字节数, 错误)。

    out：传一个 list 进来，会把真落盘的文件绝对路径追加进去（给「下载明细」用）。
    不传就完全不影响老行为 —— backfill_*.py 那边还是三个返回值。

    desc_len：**已废弃，保留只为兼容位置调用**。文件名现在按 DouK 的
    `name_length=128` 对整串截断（见 douk_detail_name），不再单独截描述。
    """
    ct = int(aweme.get("create_time") or 0)
    if not ct:
        return 0, 0, "没有 create_time"
    date = datetime.fromtimestamp(ct).strftime("%Y-%m-%d")
    # 文件名必须和 DouK 生成的一字不差，否则 DouK 下次跑这个作者时，
    # 「目标文件已存在」这一关过不去，会把同一件作品再下一份。
    # DouK 的做法是：`日期-昵称-描述` 拼成一串 → 清洗 → 整串截断到 128 宽。
    name = douk_detail_name(date, nickname, aweme.get("desc") or "",
                            aweme.get("aweme_id") or "")
    referer = "https://www.douyin.com/video/%s" % (aweme.get("aweme_id") or "")

    os.makedirs(author_dir, exist_ok=True)
    files = 0
    size = 0
    errs = []

    images = aweme.get("images") or ((aweme.get("image_post_info") or {}).get("images") or [])
    if images:
        for i, img in enumerate(images, 1):
            img = img or {}
            # 图文作品的**每一项**都可能是会动的：实况/动图。判定只看一条 ——
            # 这一项有没有 video.play_addr，有就是一段视频，url_list 只是封面帧。
            # （DouK 5.8 也是这么分的：任意一项有 video，整条作品就按「实况」走。）
            live = pick_best_video(img.get("video") or {})
            if live:
                base = os.path.join(author_dir, "%s_%d" % (name, i))
                if existing_of(base, (VIDEO_EXT,)):
                    continue
                n, ext, err = save_item(dy.cr, live, base, referer)
                if n:
                    files += 1
                    size += n
                    if out is not None:
                        out.append(base + ext)
                else:
                    errs.append("实况%d %s" % (i, err))
                continue
            urls = pick_url(img.get("url_list"))
            if not urls:
                continue
            base = os.path.join(author_dir, "%s_%d" % (name, i))
            # 静态图：后缀交给真实 Content-Type 定（可能存成 webp/gif；
            # 真拿到动图会转 mp4，见 save_item）
            if existing_of(base, IMG_EXTS + (VIDEO_EXT,)):
                continue
            n, ext, err = save_item(dy.cr, urls, base, referer)
            if n:
                files += 1
                size += n
                if out is not None:
                    out.append(base + ext)
            else:
                errs.append("图%d %s" % (i, err))
    else:
        video = aweme.get("video") or {}
        urls = pick_best_video(video)
        if not urls:
            return 0, 0, "没有可用的视频地址"
        base = os.path.join(author_dir, name)
        if existing_of(base, (VIDEO_EXT,)):
            # 已经在了：把 ID 补进 DouK 的记录表，让 DouK 以后也认这一条
            douk_mark_downloaded(aweme.get("aweme_id"))
            return 0, 0, ""          # 已经在了，不算失败
        n, ext, err = save_item(dy.cr, urls, base, referer)
        if n:
            files, size = 1, n
            if out is not None:
                out.append(base + ext)
        else:
            errs.append(err)

    if files == 0 and not errs:
        # 每个文件都已经存在（作品本来就在库里）→ 同样补记 ID
        douk_mark_downloaded(aweme.get("aweme_id"))
        return 0, 0, ""
    if files:
        # 下成功了：记进 DouK 的记录表，以后 DouK 跑这个作者会按 ID 跳过，
        # 不必依赖文件名对得上（作者改昵称/改文案时名字必然不同）
        douk_mark_downloaded(aweme.get("aweme_id"))
    return files, size, "; ".join(errs[:2])


# ---------------------------------------------------------------------------
# 请求节奏与风控闸门
#
# 为什么要这块：追更本质是拿你的登录态反复问抖音「这个作者有没有新作品」。
# 单人 15 分钟查一次很正常（等于一个人一直在刷作者主页），但「200 人 × 每
# 15 分钟 × 全天」就是每天一万九千次，是重度用户的好几倍 —— 那才会被限流。
# 这里做三件事：① 按活跃度分档，绝大多数作者根本不需要 15 分钟查一次；
# ② 每日请求硬上限；③ 连续失败立即熔断冷却，绝不在被限流时继续打。
# ---------------------------------------------------------------------------

TIER_LABEL = {"hot": "热", "warm": "温", "cold": "冷", "idle": "静", "fixed": "定"}


def tier_of(st, cfg, now=None):
    """按「这位作者上次在抖音发作品距今多久」分档，返回 (档名, 该档间隔秒数)。"""
    now = now or time.time()
    # 优先级：last_post（亲眼看到的最新作品时间）> last_ts（已处理到的最新作品）
    # > last_new（上次下到新作品的时间）。加后两个兜底是为了让升级前就存在的
    # 老状态也能立刻算准档位，不会因为缺 last_post 把活跃作者当成"静档"。
    ref = (float(st.get("last_post") or 0)
           or float(st.get("last_ts") or 0)
           or float(st.get("last_new") or 0))
    if not ref:
        return "idle", float(cfg.get("interval_idle") or 86400)
    days = (now - ref) / 86400.0
    if days <= float(cfg.get("hot_days") or 2):
        return "hot", float(cfg.get("interval_hot") or 900)
    if days <= float(cfg.get("warm_days") or 7):
        return "warm", float(cfg.get("interval_warm") or 3600)
    if days <= float(cfg.get("cold_days") or 30):
        return "cold", float(cfg.get("interval_cold") or 10800)
    return "idle", float(cfg.get("interval_idle") or 86400)


def guard(state, cfg, now=None, accounts=1):
    """风控闸门。返回 (能不能扫, 不能扫的原因)。

    冷却中或今日额度用尽就一律不许发请求 —— 宁可漏一轮，也不能在已经
    被限流的时候继续硬打，那才是把号玩坏的唯一途径。
    """
    now = now or time.time()
    cu = float(state.get("cool_until") or 0)
    if cu > now:
        left = int((cu - now) / 60 + 0.99)
        # 冷却有两种来源：账号被限流（长）、网络不通（短）。文案得分得清，
        # 否则网络抖一下就提示「被限流」，看着像号出事了。
        why = state.get("cool_reason") or "连续失败触发"
        return False, "冷却中，还要等 %d 分钟（%s）" % (left, why)
    day = time.strftime("%Y-%m-%d")
    if state.get("req_day") != day:
        state["req_day"] = day
        state["req_today"] = 0
    # 额度跟着号的数量走：有 3 个号就相当于有 3 份额度，别让单号的保守值拖住整体
    budget = int(cfg.get("daily_budget") or 0) * max(1, int(accounts or 1))
    used = int(state.get("req_today") or 0)
    if budget and used >= budget:
        return False, "今日查询额度已用完（%d 次），明天自动继续" % budget
    return True, ""


def _mark_fail(state, cfg, now=None):
    """记一次查询失败。连续失败到阈值就熔断，冷却时间逐次翻倍。"""
    now = now or time.time()
    state["consec_fail"] = int(state.get("consec_fail") or 0) + 1
    limit = int(cfg.get("break_fails") or 8)
    if state["consec_fail"] < limit:
        return False
    lvl = min(int(state.get("cool_level") or 0), 3)
    cool = float(cfg.get("break_cool") or 900) * (2 ** lvl)
    state["cool_until"] = now + cool
    state["cool_level"] = lvl + 1
    state["cool_reason"] = "疑似被限流，暂停保护"
    state["consec_fail"] = 0
    log("⚠ 连续 %d 位查询都失败，判断被抖音限流：暂停 %.0f 分钟再试（这是保护，不是故障）"
        % (limit, cool / 60))
    return True


def _mark_net_fail(state, cfg, now=None):
    """网络不通（连不上抖音 / 代理挂了）：短冷却后自己恢复，不升级、不翻倍。

    和 `_mark_fail` 分开的理由：这类失败跟「抖音在限流我」没有关系，
    按限流那套翻倍（10→20→40 分钟）只会让一次断网毁掉半天。
    """
    now = now or time.time()
    cool = float(cfg.get("net_cool") or 120)
    state["cool_until"] = now + cool
    state["cool_reason"] = "网络不通，稍后自动恢复"
    state["consec_fail"] = 0
    return True


def _mark_ok(state):
    """查询成功：连续失败计数归零，冷却级别也往下退一级（慢慢回到正常节奏）。"""
    state["consec_fail"] = 0
    if state.get("cool_level"):
        state["cool_level"] = max(0, int(state["cool_level"]) - 1)


def estimate_daily(watch, state, now=None):
    """估算名单每天会发多少次请求（面板上展示，让你看清代价）。"""
    now = now or time.time()
    cfg = watch["settings"]
    active = 0.0
    for item in watch.get("authors") or []:
        if not item.get("enabled", True):
            continue
        uid = str(item.get("uid") or "")
        st = state["authors"].get(uid) or {}
        fixed = float(item.get("interval") or 0)
        _t, iv = (("fixed", fixed) if fixed > 0 else tier_of(st, cfg, now))
        active += 86400.0 / max(60.0, iv)
    return int(active)


# ---------------------------------------------------------------------------
# 主体：一轮扫描
# ---------------------------------------------------------------------------

def _pace(cfg):
    """作者之间的间隔：基础值 + 随机抖动。固定节拍反而最像机器。"""
    base = float(cfg.get("gap") or 2.0)
    return base * (1 + float(cfg.get("gap_jitter") or 0.3) * random.random())


def scan_round(dy, watch, state, lib_idx, lib_folders, dry=False, only_uid=None,
               douk_ids=None):
    """扫一轮。返回统计 dict。

    douk_ids：DouK 下载记录里的 aweme_id 集合，传 None 就自己读一次（带 60 秒缓存）。
    这一层比文件名可靠：DouK 按 ID 判重，追更以前只看描述，描述被改过就会重下。
    """
    cfg = watch["settings"]
    if douk_ids is None:
        douk_ids = douk_id_set()
    stats = {"authors": 0, "checked": 0, "new": 0, "downloaded": 0,
             "bytes": 0, "failed": 0, "skipped": 0, "errors": [],
             "paused": "", "requests": 0, "would": 0, "empty": 0, "net": 0}
    budget = int(cfg.get("max_per_round") or 40)
    now = time.time()
    # 常驻进程可能跨午夜：每轮先把各号的「今日请求」归零，别让昨天的账算到今天头上
    pool = getattr(dy, "pool", None)
    if pool is not None:
        pool.reset_day()
    n_acc = len([a for a in (pool.accounts if pool else []) if a.get("enabled", True)]) or 1

    # 闸门一：冷却中 / 今日额度用完 → 一个请求都不发
    ok, why = guard(state, cfg, now, accounts=n_acc)
    if not ok:
        stats["paused"] = why
        log("⏸ %s，本级跳过" % why)
        return stats

    rest_every = int(cfg.get("rest_every") or 0)
    rest_secs = float(cfg.get("rest_secs") or 0)
    done = 0
    # 额度按「真实发出的请求数」记（dy.reqs 含重试），不是按「查了几位作者」。
    # 失败重试时一次查询要打 2~3 枪，只数作者会把真实消耗低估一倍以上。
    req0 = int(getattr(dy, "reqs", 0) or 0)
    base_today = int(state.get("req_today") or 0)
    base_day = state.get("req_day") or time.strftime("%Y-%m-%d")

    # dry（试运行）绝对不能改动状态：否则"看过"的作品会被记成"已处理"，
    # 真正跑的时候就被永久跳过 —— 这是会丢作品的严重问题，不是小事。
    def _save():
        if not dry:
            # 顺着刷一下心跳：一轮扫描可能跑十几分钟（翻页 + 下载），
            # 只在循环开头刷的话，网页会误报「没在跑」（阈值 180 秒）。
            state["heartbeat"] = time.time()
            save_state(state)

    def _sync_quota():
        used = int(getattr(dy, "reqs", 0) or 0) - req0
        if state.get("req_day") != base_day:
            # 一轮跑着跑着跨了午夜：前一天的额度不能带到新的一天
            state["req_today"] = used
        else:
            state["req_today"] = base_today + used
        stats["requests"] = used

    for item in watch.get("authors") or []:
        if not item.get("enabled", True):
            continue
        uid = str(item.get("uid") or "")
        sec = str(item.get("sec_uid") or "")
        nick = str(item.get("nickname") or uid)
        if not sec:
            continue
        if only_uid and uid != str(only_uid):
            continue
        stats["authors"] += 1

        # 闸门二：每位作者之前都复查一次，额度一满就立刻收手（不用等整轮跑完）
        _sync_quota()
        ok, why = guard(state, cfg)
        if not ok:
            stats["paused"] = why
            log("⏸ %s，本轮提前收手" % why)
            break

        st = state["authors"].setdefault(uid, {
            "last_ts": 0, "last_scan": "", "seen": [], "downloads": 0,
            "fails": {}, "added_at": item.get("added_at") or now_str(),
        })
        # 四档节奏：按这位作者上次发作品距今多久决定下次什么时候再看。
        # 作者自带 interval（导出页选「15 分钟」这类）就用它，不再看分档。
        tier, iv = tier_of(st, cfg, now)
        fixed = float(item.get("interval") or 0)
        if fixed > 0:
            tier, iv = "fixed", fixed
        st["tier"] = tier
        st["is_hot"] = (tier == "hot")      # 兼容旧字段（面板/旧代码还在读）
        if not dry:      # 试运行不排下一轮，否则会白白推迟真跑
            st["next_check"] = now + iv

        # 小憩：连续查了几十位之后停一下，别让请求曲线是死板的一条直线
        if rest_every and done and done % rest_every == 0 and rest_secs:
            log("· 已查 %d 位，歇 %.0f 秒" % (done, rest_secs))
            time.sleep(rest_secs)

        done += 1

        try:
            al = dy.author_works(sec, pages=int(cfg.get("per_author_pages") or 1))
        except RetryLater as e:
            # 网络断了 / 号池全在冷却：跟这位作者一点关系都没有。
            # 不进 failed、不累加 consec_fail，只等一小会儿再试，
            # 免得一次断网把整个名单判成「被限流」停 40 分钟。
            stats["net"] = int(stats.get("net") or 0) + 1
            st["last_scan"] = now_str()
            log("… %s 暂时查不了（%s），等一下再试" % (nick, str(e)[:90]))
            if stats["net"] >= 3:
                _mark_net_fail(state, cfg)
                stats["paused"] = "连不上抖音，稍后自动恢复"
                stats["net"] = 0
                log("⏸ 连续 %d 次连不上抖音：先停 %.0f 分钟（不是限流，网络通了就自己续上）"
                    % (3, float(cfg.get("net_cool") or 120) / 60))
                _save()
                break
            _save()
            time.sleep(float(cfg.get("net_wait") or 20))
            continue
        except Exception as e:
            stats["failed"] += 1
            st["last_scan"] = now_str()
            stats["errors"].append("%s 查询失败：%s" % (nick, str(e)[:80]))
            log("✗ %s 查询失败：%s" % (nick, str(e)[:100]))
            _save()
            if _mark_fail(state, cfg):
                stats["paused"] = "触发限流保护"
                _save()
                break
            time.sleep(_pace(cfg))
            continue
        _mark_ok(state)
        stats["net"] = 0
        stats["checked"] += 1
        st["last_scan"] = now_str()

        if not al:
            had_works = len(lib_idx.get(uid) or ())
            if had_works:
                # 本地明明有几十上百条，接口却返回空 —— 不是"没作品"，多半是这位作者
                # 改名/隐藏/注销，也可能抖音开始对你软限流（返回 200 + 空列表）。
                # 单个出现只当提示；连着好几位都这样，就按限流处理（换号 / 停一会儿）。
                stats["empty"] = int(stats.get("empty") or 0) + 1
                log("· %s 接口返回空（本地有 %d 条，可能被隐藏/注销；连续 5 位就会换号）"
                    % (nick, had_works))
                if stats["empty"] >= 5:
                    if pool is not None:
                        pool.mark_fail("连续 %d 位返回空列表" % stats["empty"], cfg=cfg)
                        pool.rotate("连续空列表，疑似软限流")
                    stats["empty"] = 0
            else:
                log("· %s 没有公开作品" % nick)
            _save()
            time.sleep(_pace(cfg))
            continue
        stats["empty"] = 0

        cts = [int(a.get("create_time") or 0) for a in al]
        newest = max(cts) if cts else 0
        # 记下亲眼看到的最新作品时间：分档节奏靠它，比「上次下到新作品」准得多
        if newest > int(st.get("last_post") or 0):
            st["last_post"] = newest
            tier, iv = tier_of(st, cfg)
            if st.get("tier") == "fixed" and float(item.get("interval") or 0) > 0:
                tier, iv = "fixed", float(item.get("interval"))
            st["tier"] = tier
            if not dry:
                st["next_check"] = time.time() + iv

        # 关机一夜/几天回来时，第一页可能整页都比基线新（说明漏的不止一页）。
        # 这时自动往下多翻几页，保证「漏了的那段时间」一条都不剩。
        if int(st.get("last_ts") or 0) and al and len(al) >= 18:
            if min(cts) > int(st.get("last_ts") or 0):
                try:
                    more = dy.author_works(sec, pages=int(cfg.get("backfill_pages") or 4))
                    if len(more) > len(al):
                        al = more
                        cts = [int(x.get("create_time") or 0) for x in al]
                        newest = max(cts) if cts else newest
                        log("· %s 一页装不下（整页都比上次新），已往下补翻到 %d 条"
                            % (nick, len(al)))
                except Exception as e:
                    log("· %s 补翻页失败（不影响本轮）：%s" % (nick, str(e)[:60]))
        seen = set(st.get("seen") or [])
        last_ts = int(st.get("last_ts") or 0)

        # 首次加入名单：基线定在「本地库里最后一件作品的**那一天 00:00**」。
        # 为什么不是「那天 23:59:59」，也不是「今天」：
        #   如果最后一件是今天下的、基线就变成今天 23:59:59，那今天更早发布、
        #   但当时根本没下到的那些，ct <= 基线 → 直接当老作品跳过，从此永远补不下来。
        #   取当天起点最保守：当天作品全部重新核对，已下过的由第 3 层判重挡掉，不会重复下。
        # 本地一件作品都没有：基线归零 → 抖音上现有作品全量过一遍（这才叫"追更"）。
        #   一轮有下载上限，几千条的作者不会一次全拉，last_ts 随下载推进，下轮接着下。
        if last_ts == 0:
            local_set = lib_idx.get(uid) or set()
            lib_latest = max((d for d, _k in local_set), default="")
            if lib_latest:
                try:
                    base = int(time.mktime(time.strptime(lib_latest, "%Y-%m-%d")))
                except ValueError:
                    base = 0
            else:
                base = 0
                # 本地一件都没有 = 需要「首次全量补齐」：见下面 st["bulk"] 的处理。
                # 不能只靠 last_ts=0 那一轮：候选按时间升序下，下到最新的那条之后
                # last_ts 就被推到最新，比它更老的（第 2 页往后）永远 ct <= last_ts 被跳过。
                st["bulk"] = True
            st["last_ts"] = int(base)
            # 只把「基线之前」的作品记成处理过；比基线新的留着当候选去下载
            st["seen"] = [str(a.get("aweme_id")) for a in al
                          if int(a.get("create_time") or 0) <= base][:int(cfg["seen_keep"])]
            st["last_new"] = 0
            miss = sum(1 for a in al if int(a.get("create_time") or 0) > base)
            say_base = (datetime.fromtimestamp(base).strftime("%m-%d 00:00")
                        if base else "全量（本地还没有作品）")
            log("＋ %s 建立基线：%s（这页 %d 条）%s"
                % (nick, say_base, len(al),
                   "，缺 %d 条会马上补下" % miss if miss else "，之后有新作品就下"))
            _save()   # 必须存：不存的话下次重启又把基线当 0，白查一遍
            last_ts = int(base)
            seen = set(st.get("seen") or [])
            # 不 continue：紧跟着就用新基线筛候选，本地缺的那几条这一轮就补下，
            # 不用等下一轮（不然点完「自动追更」要等十几分钟才动）。

        local = lib_idx.get(uid) or set()
        date_idx = build_date_index(local)
        folder = lib_folders.get(uid) or ""
        author_dir = os.path.join(paths.SCAN_ROOT, folder) if folder else ""
        # 老状态补丁（2026-11-09）：已经有基线、但本地一件作品都没有的作者，
        # 基线是旧算法按「抖音最新那条」定的 → 永远不会有候选（ct <= last_ts 全跳过）。
        # 首轮遇到就转成一次性「全量补齐」；bulk_done 标记保证只做一次。
        # 两个前提：① lib_idx 非空（索引没建好时不能判断，否则会把全名单都标成全量）；
        #          ② 有作者文件夹（没文件夹就没地方落盘，补了也是白补）。
        if (not st.get("bulk") and not local and lib_idx and author_dir
                and not st.get("bulk_done")):
            st["bulk"] = True
            st["last_ts"] = 0
            st["seen"] = []
            last_ts = 0
            seen = set()
            log("· %s 本地一件作品都没有，改成先把现有作品整体补一遍" % nick)

        # 首次全量补齐（本地一件作品都没有的作者）：多翻几页，把现有作品整体过一遍。
        # 作者还没有文件夹时不深翻 —— 下了也没地方放，白白多打几枪接口。
        if st.get("bulk") and author_dir:
            try:
                more = dy.author_works(sec, pages=int(cfg.get("bulk_pages") or 10))
                if len(more) > len(al):
                    al = more
                    cts = [int(x.get("create_time") or 0) for x in more]
                    newest = max(cts) if cts else newest
                    log("· %s 首次全量：往下翻到 %d 条作品" % (nick, len(al)))
            except Exception as e:
                log("· %s 全量翻页失败（不影响本轮，下轮再补）：%s" % (nick, str(e)[:60]))

        # 候选：比基线新、且没处理过的（「首次全量」阶段不看基线，只看有没有处理过）
        want_content = str(cfg.get("content") or "all").lower()
        bulk = bool(st.get("bulk"))
        cand = []
        for a in al:
            aid = str(a.get("aweme_id") or "")
            ct2 = int(a.get("create_time") or 0)
            if not aid or aid in seen:
                continue
            # DouK 已经下过这一条：不管它当时存的文件名是什么、现在描述改没改，
            # 都不再下一份。记录里有而磁盘上找不到，说明是用户自己删的 ——
            # 按「删了就别再补回来」处理（和 tools/backfill_author.py 同一口径）。
            # 2026-11-09 的「🥲#卡点舞」就是这么重复的：DouK 那份叫 `#卡点舞`，
            # 接口把描述改成了带表情的版本，只比文件名永远对不上。
            if douk_ids and aid in douk_ids:
                seen.add(aid)
                stats["skipped"] += 1
                continue
            if not bulk and ct2 <= last_ts:
                continue
            if (st.get("fails") or {}).get(aid, 0) >= int(cfg.get("fail_limit") or 3):
                seen.add(aid)
                continue
            # 追更内容设成「只追视频 / 只追图文」时，另一类直接算处理过：
            # 不下载不算失败，但**基线要继续往前走**，否则每一轮都会重新「发现」同一批
            if want_content in ("video", "image") and aweme_kind(a) != want_content:
                seen.add(aid)
                st["last_ts"] = max(int(st.get("last_ts") or 0), ct2)
                stats["skipped"] += 1
                continue
            cand.append((ct2, a))
        cand.sort(key=lambda x: x[0])   # 老的先下，顺序跟时间线一致

        if not cand:
            # 没有可下的作品（正常模式：没有比基线更新的；首次全量：这一批都处理过了）
            if st.get("bulk") and not dry:
                st["bulk"] = False
                st["bulk_done"] = True
                st["last_ts"] = max(int(st.get("last_ts") or 0), newest)
                log("✔ %s 首次全量补齐完成（现有作品都过了一遍），之后按新作品追" % nick)
            _save()
            time.sleep(_pace(cfg))
            continue

        stats["new"] += len(cand)
        log("★ %s 发现 %d 条%s（最新 %s）"
            % (nick, len(cand), "待补作品" if bulk else "新作品",
               datetime.fromtimestamp(max(c[0] for c in cand)).strftime("%m-%d %H:%M")))

        no_folder = False
        budget_hit = False
        for ct2, a in cand:
            if budget <= 0:
                log("· 本轮达到下载上限（%d 条），剩下的下一轮再下" % cfg["max_per_round"])
                budget_hit = True
                break
            aid = str(a.get("aweme_id") or "")
            date = datetime.fromtimestamp(ct2).strftime("%Y-%m-%d")
            desc = a.get("desc") or ""
            dl = int(cfg.get("desc_len") or 64)
            # 第 3 层判重：库里已有同一天同一描述 → DouK 以前下过
            # 老文件名里的 `#...` 由 is_known 按前后缀认出，不会重复下载
            if is_known(local, date_idx, date, desc, dl):
                seen.add(aid)
                st["last_ts"] = max(int(st.get("last_ts") or 0), ct2)
                stats["skipped"] += 1
                continue
            if not author_dir:
                # 这位作者本地库里还没有文件夹：不建目录（避免和库里的作者对不上），
                # 但**绝不能算"下载失败"** —— 失败 3 次就会进 fails、被标记已处理，
                # 那些作品以后永远下不到了。这里只跳过，等作者进库后自然补上。
                stats["skipped"] += 1
                no_folder = True
                continue
            if dry:
                log("  [试运行] 会下载 %s  %s" % (aid, (desc or "")[:30]))
                stats["would"] = int(stats.get("would") or 0) + 1
                continue

            _got = []
            files, size, err = download_aweme(dy, a, author_dir, nick, dl, out=_got)
            if files:
                budget -= 1
                stats["downloaded"] += 1
                stats["bytes"] += size
                st["downloads"] = int(st.get("downloads") or 0) + 1
                st["last_new"] = time.time()
                st["last_ts"] = max(int(st.get("last_ts") or 0), ct2)
                # 记流水：主界面的「累计追更下载」点开就是这份明细
                try:
                    _rels = [os.path.relpath(p, paths.SCAN_ROOT).replace(os.sep, "/")
                             for p in _got]
                    record_download(uid, nick, date, desc, _rels, size)
                except Exception as _e:      # 记账失败不影响下载结果
                    print("[watch] 记下载明细失败：%s" % _e)
                seen.add(aid)
                k_new = (date, key_of_desc(desc, dl))
                local.add(k_new)
                date_idx.setdefault(date, []).append(k_new[1])
                log("  ↓ %s  %s (%.1f MB)"
                    % (datetime.fromtimestamp(ct2).strftime("%m-%d %H:%M"),
                       (desc or "")[:34], size / 1048576.0))
            elif err:
                stats["failed"] += 1
                st.setdefault("fails", {})[aid] = st.get("fails", {}).get(aid, 0) + 1
                stats["errors"].append("%s %s：%s" % (nick, aid, err[:70]))
                log("  ✗ %s 下载失败：%s" % ((desc or aid)[:26], err[:90]))
            else:
                # 文件已存在：算处理过了
                seen.add(aid)
                st["last_ts"] = max(int(st.get("last_ts") or 0), ct2)
                stats["skipped"] += 1

        if no_folder:
            # 只在每轮说一次，别为每条作品刷屏；1 小时后再看，作者进库后会自动补上
            stats["errors"].append("%s 还不在本地库，跳过 %d 条（不会丢，进库后自动补）"
                                   % (nick, len(cand)))
            log("· %s 还不在本地库，跳过 %d 条（作品不会丢，等它进库后自动补）"
                % (nick, len(cand)))
            if not dry:
                st["next_check"] = time.time() + 3600
            # 连文件夹都没有：这次全量补不了，把标记摘掉。
            # last_ts 仍是 0，等它真的进库后，下一轮会重新建基线 → 那时才走全量。
            if st.get("bulk"):
                st["bulk"] = False
                st["bulk_done"] = True

        # 「首次全量」收尾：
        #   这一轮没被下载额度打断 → 现有作品都过了一遍，切回正常追更（基线推到最新）；
        #   被打断 → 5 分钟后再来一批，别让它按档位睡到十几小时后才补下一批。
        #   作者还没有文件夹（no_folder）时不动它：等它进库了再补。
        if st.get("bulk") and not dry and not no_folder:
            if budget_hit:
                st["next_check"] = time.time() + 300
                log("· %s 首次全量还没补完，5 分钟后接着补" % nick)
            else:
                st["bulk"] = False
                st["bulk_done"] = True
                st["last_ts"] = max(int(st.get("last_ts") or 0), newest)
                log("✔ %s 首次全量补齐完成（现有作品都过了一遍），之后按新作品追" % nick)

        # 记忆裁剪
        st["seen"] = list(seen)[-int(cfg.get("seen_keep") or 400):]
        # 失败计数**不能**按 seen 过滤：失败的 id 本来就不在 seen 里（要留着下轮重试），
        # 一过滤就等于「每次都当第一次失败」，永远到不了上限。只截长度。
        fs = st.get("fails") or {}
        if len(fs) > 200:
            st["fails"] = dict(list(fs.items())[-200:])
        _save()        # 每个作者存一次：中途断电不会把整轮的白跑
        time.sleep(_pace(cfg))

    _sync_quota()
    return stats


def which_authors_due(watch, state, now=None):
    """按分级算这一轮该查哪些作者（常驻循环用）。"""
    now = now or time.time()
    due = []
    for item in watch.get("authors") or []:
        if not item.get("enabled", True):
            continue
        uid = str(item.get("uid") or "")
        st = state["authors"].get(uid) or {}
        nxt = float(st.get("next_check") or 0)
        if nxt <= now:
            due.append(item)
    return due
