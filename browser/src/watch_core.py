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
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

WATCH_FILE = os.path.join(paths.DATA_DIR, "watch.json")
STATE_FILE = os.path.join(paths.DATA_DIR, "watch_state.json")
LOG_FILE = os.path.join(paths.DATA_DIR, "watch.log")
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
    "account_ban": 300,         # 单个号连续失败后先歇 5 分钟，再犯翻倍
    "max_per_round": 40,        # 单轮最多下载多少条，防止一次性爆量
    "desc_len": 64,             # 描述截断长度，对齐 DouK 的 desc_length
    "fail_limit": 3,            # 同一条作品失败 N 次后放弃，避免卡死
    "seen_keep": 400,           # 每个作者保留多少条 aweme_id 记忆
}

VIDEO_EXT = ".mp4"
IMG_EXT_BY_TYPE = {
    "image/jpeg": ".jpeg", "image/jpg": ".jpeg", "image/png": ".png",
    "image/webp": ".webp", "image/heic": ".heic",
}

# Windows 文件名不允许的字符。DouK 也没法保留这些，一律换成下划线。
BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")


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
    """先写临时文件再换名：中途断电不会留下半个 JSON（下一轮就读不出来了）。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


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


def work_key(desc, desc_len=64):
    """判重 key 的统一口径：**先按 DouK 规则清洗截断，再规范化**。

    顺序不能反。库里存的是文件名里那段（已经截断到 64 字），接口给的是完整描述；
    如果直接拿完整描述去比，超过 64 字的作品永远对不上，每天都会被当成新的下一遍。
    """
    return norm_desc(clean_part(desc or "", desc_len))


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
    return d


def save_state(d):
    _write_json(STATE_FILE, d)


# ---------------------------------------------------------------------------
# 本地库索引：判断「这条作品是不是早就有了」
# ---------------------------------------------------------------------------

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
        s = set()
        for w in (a.get("works") or []):
            date = str(w.get("date") or "")
            desc = w.get("desc")
            if desc is None:
                # scanner 有些老记录把 desc 存在文件名里，用 files 兜底
                rel = ((w.get("files") or [{}])[0] or {}).get("rel") or ""
                desc = parse_desc_from_name(rel.split("/")[-1])
            s.add((date, work_key(desc)))
        idx[uid] = s
    return idx, folders


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
            raise RuntimeError("账号池里没有可用的号（都没登录或全在冷却）")
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
            raise RuntimeError("账号池里没有可用的号（都在冷却或没登录）")
        q = self.ff.build_query(self.bi)
        q.update(extra)
        q["a_bogus"] = self.ab.get_value(
            self.ff.urlencode(q), body="",
            content_type="application/x-www-form-urlencoded")
        self.reqs += 1
        acct["req_today"] = int(acct.get("req_today") or 0) + 1
        return self.cr.get(url, params=q,
                           headers=self.ff.make_headers(self.current_cookie(), referer),
                           impersonate="chrome146", timeout=25)

    def author_works(self, sec_uid, pages=1, retry=2):
        """拉作者主页作品列表（新→旧）。返回 aweme_list。

        失败时**先换号再重试**：同一个号上连着打三四枪既慢又最容易被盯上，
        换个号往往一次就过。只有池子里没有别的号可用时才原地等。
        """
        out, cursor, seen = [], "0", set()
        for _ in range(max(1, pages)):
            last = ""
            data = None
            for attempt in range(1, max(1, retry) + 1):
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
                    self.pool.mark_fail(last)
                if attempt < max(1, retry):
                    # 换号成功就几乎不用等；没号可换才退让一下
                    if self.pool.rotate(last):
                        self.switches += 1
                        time.sleep(0.5)
                    else:
                        time.sleep(attempt * 1.5)
            if not data:
                if out:
                    break
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
    """url_list 一般有 2~3 个镜像，取第一个能用的（调用方负责失败重试下一个）。"""
    return [u for u in (url_list or []) if isinstance(u, str) and u.startswith("http")]


def pick_best_video(video):
    """挑最高码率的 play_addr。

    抖音返回的 video.bit_rate 里按码率排了一串，play_addr 是「默认清晰度」。
    追更下的量不大，尽量拿好的那个：先看 bit_rate，再退回 play_addr。
    """
    best, best_score = None, -1
    for br in (video.get("bit_rate") or []):
        addr = br.get("play_addr") or {}
        urls = pick_url(addr.get("url_list"))
        if not urls:
            continue
        score = (addr.get("data_size") or 0) or br.get("bit_rate") or 0
        if score > best_score:
            best_score, best = score, urls
    if best:
        return best
    return pick_url((video.get("play_addr") or {}).get("url_list"))


def _ext_from_type(ct):
    ct = (ct or "").split(";")[0].strip().lower()
    return IMG_EXT_BY_TYPE.get(ct, "")


def save_stream(cr, urls, dest, referer, retry=3):
    """把 urls（按顺序试）流式写到 dest。先写 .part 再改名。

    为什么要 .part：scanner 和 DouK 都在监听目录，半截文件被扫进去会记成
    「下载失败」或算错体积。改名是原子的，写完才让它出现。
    """
    tmp = dest + ".part"
    last = ""
    for attempt in range(1, retry + 1):
        for u in urls:
            try:
                r = cr.get(u, headers={"User-Agent": UA, "Referer": referer},
                           impersonate="chrome146", timeout=60, stream=True)
                if r.status_code != 200:
                    last = "HTTP %s" % r.status_code
                    r.close()
                    continue
                total = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(262144):
                        if chunk:
                            f.write(chunk)
                            total += len(chunk)
                r.close()
                if total < 1024:
                    last = "只拿到 %d 字节" % total
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    continue
                os.replace(tmp, dest)
                return total, ""
            except Exception as e:
                last = "%s: %s" % (type(e).__name__, str(e)[:80])
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
        time.sleep(attempt * 2)
    return 0, last


def download_aweme(dy, aweme, author_dir, nickname, desc_len=64):
    """下一条作品到 author_dir。返回 (文件数, 字节数, 错误)。"""
    ct = int(aweme.get("create_time") or 0)
    if not ct:
        return 0, 0, "没有 create_time"
    date = datetime.fromtimestamp(ct).strftime("%Y-%m-%d")
    desc = clean_part(aweme.get("desc") or "", desc_len) or str(aweme.get("aweme_id") or "")
    nick = clean_part(nickname) or "未知作者"
    referer = "https://www.douyin.com/video/%s" % (aweme.get("aweme_id") or "")

    os.makedirs(author_dir, exist_ok=True)
    files = 0
    size = 0
    errs = []

    images = aweme.get("images") or ((aweme.get("image_post_info") or {}).get("images") or [])
    if images:
        for i, img in enumerate(images, 1):
            urls = pick_url((img or {}).get("url_list"))
            if not urls:
                continue
            # 扩展名：先按 URL 猜，猜不到就用 content-type 兜底（save_stream 之后才知道）
            ext = ".jpeg"
            low = urls[0].lower()
            for cand in (".jpeg", ".jpg", ".png", ".webp", ".heic"):
                if cand in low:
                    ext = ".jpeg" if cand == ".jpg" else cand
                    break
            name = "%s-%s-%s_%d%s" % (date, nick, desc, i, ext)
            dest = os.path.join(author_dir, name)
            if os.path.exists(dest) and os.path.getsize(dest) > 1024:
                continue
            n, err = save_stream(dy.cr, urls, dest, referer)
            if n:
                files += 1
                size += n
            else:
                errs.append("图%d %s" % (i, err))
    else:
        video = aweme.get("video") or {}
        urls = pick_best_video(video)
        if not urls:
            return 0, 0, "没有可用的视频地址"
        name = "%s-%s-%s%s" % (date, nick, desc, VIDEO_EXT)
        dest = os.path.join(author_dir, name)
        if os.path.exists(dest) and os.path.getsize(dest) > 1024:
            return 0, 0, ""          # 已经在了，不算失败
        n, err = save_stream(dy.cr, urls, dest, referer)
        if n:
            files, size = 1, n
        else:
            errs.append(err)

    if files == 0 and not errs:
        return 0, 0, ""
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
        return False, "限流冷却中，还要等 %d 分钟（连续失败触发）" % left
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
    state["consec_fail"] = 0
    log("⚠ 连续 %d 位查询都失败，判断被抖音限流：暂停 %.0f 分钟再试（这是保护，不是故障）"
        % (limit, cool / 60))
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


def scan_round(dy, watch, state, lib_idx, lib_folders, dry=False, only_uid=None):
    """扫一轮。返回统计 dict。"""
    cfg = watch["settings"]
    stats = {"authors": 0, "checked": 0, "new": 0, "downloaded": 0,
             "bytes": 0, "failed": 0, "skipped": 0, "errors": [],
             "paused": "", "requests": 0, "would": 0, "empty": 0}
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

        # 首次加入名单：基线定在「本地库里最新那件作品」，不是「现在」。
        # 这样你点完「自动追更」，本地缺的近期作品会补下来（这才叫追更）；
        # 只有本地一件都没有的作者才退化成「从现在开始」，免得一加进来就爆下上千条。
        if last_ts == 0:
            local_set = lib_idx.get(uid) or set()
            lib_latest = max((d for d, _k in local_set), default="")
            base = newest
            if lib_latest:
                try:
                    base = int(time.mktime(time.strptime(lib_latest, "%Y-%m-%d")) + 86400 - 1)
                except ValueError:
                    base = newest
            st["last_ts"] = int(base)
            # 只把「基线之前」的作品记成处理过；比基线新的留着当候选去下载
            st["seen"] = [str(a.get("aweme_id")) for a in al
                          if int(a.get("create_time") or 0) <= base][:int(cfg["seen_keep"])]
            st["last_new"] = 0
            miss = sum(1 for a in al if int(a.get("create_time") or 0) > base)
            log("＋ %s 建立基线：%s（%d 条）%s"
                % (nick,
                   datetime.fromtimestamp(base).strftime("%m-%d %H:%M") if base else "—",
                   len(al),
                   "，本地缺 %d 条会马上补下" % miss if miss else "，之后有新作品就下"))
            _save()   # 必须存：不存的话下次重启又把基线当 0，白查一遍
            last_ts = int(base)
            seen = set(st.get("seen") or [])
            # 不 continue：紧跟着就用新基线筛候选，本地缺的那几条这一轮就补下，
            # 不用等下一轮（不然点完「自动追更」要等十几分钟才动）。

        local = lib_idx.get(uid) or set()
        folder = lib_folders.get(uid) or ""
        author_dir = os.path.join(paths.SCAN_ROOT, folder) if folder else ""

        # 候选：比基线新、且没处理过的
        cand = []
        for a in al:
            aid = str(a.get("aweme_id") or "")
            ct2 = int(a.get("create_time") or 0)
            if not aid or aid in seen or ct2 <= last_ts:
                continue
            if (st.get("fails") or {}).get(aid, 0) >= int(cfg.get("fail_limit") or 3):
                seen.add(aid)
                continue
            cand.append((ct2, a))
        cand.sort(key=lambda x: x[0])   # 老的先下，顺序跟时间线一致

        if not cand:
            # 没有比基线更新的作品：什么也不用记，位置留着
            _save()
            time.sleep(_pace(cfg))
            continue

        stats["new"] += len(cand)
        log("★ %s 发现 %d 条新作品（最新 %s）"
            % (nick, len(cand),
               datetime.fromtimestamp(max(c[0] for c in cand)).strftime("%m-%d %H:%M")))

        no_folder = False
        for ct2, a in cand:
            if budget <= 0:
                log("· 本轮达到下载上限（%d 条），剩下的下一轮再下" % cfg["max_per_round"])
                break
            aid = str(a.get("aweme_id") or "")
            date = datetime.fromtimestamp(ct2).strftime("%Y-%m-%d")
            desc = a.get("desc") or ""
            dl = int(cfg.get("desc_len") or 64)
            # 第 3 层判重：库里已有同一天同一描述 → DouK 以前下过
            if (date, work_key(desc, dl)) in local:
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

            files, size, err = download_aweme(dy, a, author_dir, nick, dl)
            if files:
                budget -= 1
                stats["downloaded"] += 1
                stats["bytes"] += size
                st["downloads"] = int(st.get("downloads") or 0) + 1
                st["last_new"] = time.time()
                st["last_ts"] = max(int(st.get("last_ts") or 0), ct2)
                seen.add(aid)
                local.add((date, work_key(desc, dl)))
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
