#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音下载浏览器 - 本地 HTTP 服务
- 托管网页（index.html / app.js / style.css）
- 托管媒体文件（downnnnnnnn 目录，支持视频/图片播放）
- 提供 /api/save 保存用户标注
- 提供 /api/open 在资源管理器打开文件
- 提供 /api/rescan + /api/rescan_status 触发重新扫描并查询真实进度
- 提供 /api/transcode + /api/transcode_status 异步转码并查询进度
"""

import os
import sys
import json
import hashlib
import subprocess
import functools
import threading
import time
import glob
import random
import shutil
import queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import traceback
import urllib.parse
import urllib.request
from urllib.parse import urlparse, unquote, parse_qs

# 写 user_data.json 的互斥锁：ThreadingHTTPServer 是多线程的，多个保存请求
# 并发到达会同时读写同一文件，导致互相覆盖甚至写出半截 JSON。所有「读-改-写」
# 必须作为一个整体持锁执行（见 merge_save_user_data），否则并发保存仍会丢更新。
_WRITE_LOCK = threading.Lock()

# 内存索引的锁。ThreadingHTTPServer 是多线程的：一个请求线程在
# `for uid, a in CACHE["accounts"].items()` 遍历时，另一个线程重建索引，
# 会抛 RuntimeError: dictionary changed size during iteration（表现为随机 500 / 空响应）。
# 规则：重建一律在 _CACHE_LOCK 内完成，并且只替换引用、不原地 append。
_CACHE_LOCK = threading.RLock()


class UserDataError(RuntimeError):
    """标注文件读不出/结构坏，拒绝写盘。宁可这次保存失败，也不能把整库覆盖掉。"""


def merge_save_user_data(partial):
    """在锁内完成「读磁盘 → 合并 body → 归一化 → 原子写」，返回修正的 alt 条数。

    partial 是前端 POST 的 body（含 accounts 和可选 alt_group_names）。
    合并语义：partial.accounts 里出现的账号按字段 merge 进磁盘现有；没出现的保留。
    锁必须覆盖整个读-改-写，否则并发请求各读各写，后写的覆盖先写的（丢更新）。
    """
    with _WRITE_LOCK:
        _auto_backup()          # 写之前先留一份快照（5 分钟一次）
        try:
            with open(USER_DATA, "r", encoding="utf-8") as fp:
                disk = json.load(fp)
        except FileNotFoundError:
            disk = {}           # 首次运行，允许新建
        except (OSError, ValueError) as e:
            # 关键：文件存在却读不出来（被杀毒/索引器占用、JSON 只写了半截）时，
            # **绝不能退化成空库继续写**。否则一次瞬时 OSError 就会把整份标注
            # 覆盖成只剩本次提交的那几个账号，正是历史丢数据事故的形态。
            raise UserDataError("user_data.json 读取失败，已中止保存：%s" % e)
        if not isinstance(disk, dict):
            raise UserDataError("user_data.json 顶层不是对象，已中止保存")
        # 结构自愈：正确结构是 {"accounts": {uid: 标注}}。曾有外部脚本把 accounts
        # 的内容误写成顶层（丢包装层），这里检测到就自动包回，别让坏结构扩散。
        if "accounts" not in disk and disk:
            disk = {"accounts": disk}

        merged = dict(disk.get("accounts") or {})
        for uid, ua in (partial.get("accounts") or {}).items():
            # ua 为 null = **把整个账号从标注里删掉**（配合「删除博主 + 加入黑名单」用）。
            # 只删某个字段是 {字段: null}，两者语义不同，别混：
            # 账号删了以后这个 uid 在 user_data 里彻底消失，重扫后不会再冒成合成账号。
            if ua is None:
                merged.pop(uid, None)
                continue
            base = dict(merged.get(uid) or {})
            if isinstance(ua, dict):
                for k, v in ua.items():
                    # null = 显式删除该字段（合并写无法靠「键不存在」表达删除，
                    # 否则解除大小号关联会被磁盘旧值顶回来，表现为「解除了还在」）
                    if v is None:
                        base.pop(k, None)
                    else:
                        base[k] = v
            merged[uid] = base
        disk["accounts"] = merged

        if isinstance(partial.get("alt_group_names"), dict):
            names = dict(disk.get("alt_group_names") or {})
            for k, v in partial["alt_group_names"].items():
                # null / 空串 = 显式删除这个自定义文件夹名。合并写无法用「键不存在」
                # 表达删除，否则前端清空名字后刷新又变回旧名字。
                if v is None or (isinstance(v, str) and not v.strip()):
                    names.pop(k, None)
                else:
                    names[k] = v
            disk["alt_group_names"] = names

        # 账号状态说明：{ 状态名: 用户自己写的理解 }，和 accounts 平级。
        # 按 key 合并（前端只发改过的那个状态），null / 空串 = 删除。
        # 这是用户一个一个字敲的内容，绝不能因为某次没带上就整份清掉。
        if isinstance(partial.get("status_notes"), dict):
            notes = dict(disk.get("status_notes") or {})
            for k, v in partial["status_notes"].items():
                if not isinstance(k, str) or not k:
                    continue
                if v is None or (isinstance(v, str) and not v.strip()):
                    notes.pop(k, None)
                elif isinstance(v, str):
                    notes[k] = v
            if notes:
                disk["status_notes"] = notes
            else:
                disk.pop("status_notes", None)

        # 不变式：状态属于「不能设置更新频率」的（默认注销/禁言封号/卖号）账号，不许留 update_freq。
        # 以前只在「设置里改禁用列表」时清一次；用户后来把某个号改成「禁言/封号」，旧频率还留在
        # user_data 里，侧边栏「更新频率 day」照样能筛到他（2026-10-30 报的问题）。
        # 放在保存的唯一入口兜底：前端漏清、老数据残留、别的脚本写入，都会被这里纠正。
        try:
            _disabled = {str(x).strip() for x in (load_settings().get("update_freq_disabled_status") or [])
                         if str(x).strip()}
        except Exception:
            _disabled = set()
        if _disabled:
            for ua in merged.values():
                if not isinstance(ua, dict):
                    continue
                if str(ua.get("status") or "").strip() in _disabled and ua.get("update_freq"):
                    ua.pop("update_freq", None)

        fixed = normalize_alt_groups(disk["accounts"])

        import tempfile
        # 临时文件放到标注同目录（os.replace 无需跨盘），名字唯一，避免并发互抢
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(USER_DATA), prefix=".user_data.", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                json.dump(disk, fp, ensure_ascii=False, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_path, USER_DATA)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return fixed

# ============ 路径配置 ============
import paths

# 黑名单（不下载名单）模块在 ../tools/ 下。它自己在头部把 src/ 加进 sys.path
# 后 import paths，所以这里先把 tools/ 加进 sys.path 再 import 即可。
_TOOLS_DIR = os.path.join(paths.APP_ROOT, "tools")
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
import blacklist as _BL  # noqa: E402
import download_db  # noqa: E402  下载记录库（作者最后一次下载时间 / 文件数）
import watch_core as WC  # noqa: E402  追更：名单/状态的读写（纯 JSON，3.13 也能 import；
                         # 真正联网扫描的部分在 watch.py，必须由 3.12 子进程跑）

BASE_DIR = paths.MEDIA_BASE
SCAN_ROOT = paths.SCAN_ROOT


# ============ 用户设置（data/settings.json） ============
# 存「更新频率禁用状态」「数据库/FFmpeg/组件地址」等可配置项。
# 路径类配置只做**回显与校验**——真正生效的路径仍来自 paths.py（改 settings 里的
# 地址会立即影响抓收藏/转码等用到的地方，见 get_setting 在 FFMPEG 等处的读取）。
SETTINGS_FILE = paths.SETTINGS_FILE

# 所有账号状态（与前端 STATUS_ORDER / 详情页 statusList 对齐）。
ALL_STATUS = ["正常", "私密账号", "注销", "禁言/封号", "停更/偶更", "卖号", "不好看了", "待修改"]
# 2026-10-31：'停更/偶尔更' 缩短成 '停更/偶更'。存量已迁移，这里兜住老 settings.json / 老备份带回来的旧值
STATUS_ALIAS = {"停更/偶尔更": "停更/偶更", "停更": "停更/偶更"}

# 默认「不能设置更新频率」的账号状态（用户要求：注销/禁言/封号/卖号等不能设更新频率）
DEFAULT_FREQ_DISABLED_STATUS = ["注销", "禁言/封号", "卖号"]

DEFAULT_SETTINGS = {
    # 不能设更新频率的账号状态（这些账号没有「更新」可言）
    "update_freq_disabled_status": DEFAULT_FREQ_DISABLED_STATUS,
    # 「最新作品时间」口径：none=用收藏作品发布时间 / downloaded=只查已下载作者主页 / all=所有作者都查主页
    "fav_latest_mode": "none",
    # 路径类配置（用户可在设置里改；空串 = 沿用 paths.py 默认值）
    "paths": {
        "data_dir": "",          # 数据库地址（data.json 所在目录）
        "ffmpeg": "",            # FFmpeg 地址
        "downloader_dir": "",    # 需要的组件地址（DouK-Downloader，含 curl_cffi/ABogus）
    },
}

_SETTINGS_CACHE = None
_SETTINGS_LOCK = threading.RLock()   # 可重入：save_settings 持锁后还会调 load_settings


def load_settings(force=False):
    """读 settings.json（进程内缓存）。缺失/损坏时回退默认值，不抛异常。"""
    global _SETTINGS_CACHE
    with _SETTINGS_LOCK:
        if _SETTINGS_CACHE is not None and not force:
            return _SETTINGS_CACHE
        s = dict(DEFAULT_SETTINGS)
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fp:
                disk = json.load(fp)
            if isinstance(disk, dict):
                # 按 DEFAULT_SETTINGS 的类型通用合并：以前这里逐个 key 手写，
                # 结果 fav_latest_mode 只写不读——设置里选的口径一重启就悄悄回默认，
                # 文件里明明有值（就是「代码和配置文件对不上」那种怪事）。
                for k, dv in DEFAULT_SETTINGS.items():
                    if k == "paths":
                        continue          # 路径单独处理（见下）
                    val = disk.get(k)
                    if isinstance(dv, list) and isinstance(val, list):
                        s[k] = val
                    elif isinstance(dv, str) and isinstance(val, str):
                        s[k] = val
                p = disk.get("paths")
                if isinstance(p, dict):
                    for k in s["paths"]:
                        if isinstance(p.get(k), str):
                            s["paths"][k] = p[k]
        except (OSError, ValueError):
            pass
        _SETTINGS_CACHE = s
        return s


def save_settings(partial):
    """合并写 settings.json（按 key 合并，null 删除），返回最终 settings。"""
    with _SETTINGS_LOCK:
        s = load_settings(force=True)
        new_disabled = None
        if isinstance(partial.get("update_freq_disabled_status"), list):
            new_disabled = []
            for x in partial["update_freq_disabled_status"]:
                if not isinstance(x, str):
                    continue
                _v = STATUS_ALIAS.get(x.strip(), x.strip())
                if _v in ALL_STATUS and _v not in new_disabled:
                    new_disabled.append(_v)
            s["update_freq_disabled_status"] = new_disabled
        if isinstance(partial.get("fav_latest_mode"), str):
            m = partial["fav_latest_mode"].strip()
            if m in ("none", "downloaded", "all"):
                s["fav_latest_mode"] = m
        if isinstance(partial.get("paths"), dict):
            for k, v in partial["paths"].items():
                if k in s["paths"] and isinstance(v, str):
                    s["paths"][k] = v.strip()
        # 原子写
        import tempfile
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(SETTINGS_FILE), prefix=".settings.", suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                json.dump(s, fp, ensure_ascii=False, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_path, SETTINGS_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        _SETTINGS_CACHE = s
        # 「不能设置更新频率」的状态列表变了：把这些状态的账号已有的 update_freq 属性清掉，
        # 不只是前端显示「永不更新」文字，而是真正把属性取消（注销/封号/卖号等没有更新可言）。
        if new_disabled is not None:
            _clear_freq_for_disabled_status(new_disabled)
        return s


def _clear_freq_for_disabled_status(disabled_statuses):
    """把状态命中禁用列表的账号的 update_freq 属性清掉（写回 user_data.json + 内存缓存）。

    设置里勾了「注销/禁言封号/卖号等不能设更新频率」后，这些账号若之前设过更新频率，
    会残留一个失效的 day/3天/周/月，这里一次性清空，让「永不更新」名副其实。
    """
    disabled = set(disabled_statuses or [])
    if not disabled:
        return
    with _WRITE_LOCK:
        try:
            with open(USER_DATA, "r", encoding="utf-8") as fp:
                disk = json.load(fp)
        except (OSError, ValueError):
            return   # 读不出就不动标注，别因清频率把整库搞坏
        if not isinstance(disk, dict):
            return
        accounts = disk.get("accounts") or {}
        changed = False
        for uid, ua in accounts.items():
            if not isinstance(ua, dict):
                continue
            st = str(ua.get("status") or "").strip()
            if st in disabled and ua.get("update_freq"):
                ua.pop("update_freq", None)
                changed = True
        if not changed:
            return
        import tempfile
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=os.path.dirname(USER_DATA), prefix=".user_data.", suffix=".tmp")
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
                json.dump(disk, fp, ensure_ascii=False, indent=2)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp_path, USER_DATA)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    # 同步内存缓存（下次 /api/authors 直接反映，不用重启）
    with _CACHE_LOCK:
        for uid, a in (CACHE.get("accounts") or {}).items():
            if isinstance(a, dict) and str(a.get("status") or "").strip() in disabled:
                a.pop("update_freq", None)


def effective_path(kind, default):
    """取某类路径的「生效值」：设置里填了就优先用，否则用 paths.py 默认值。"""
    s = load_settings()
    v = (s.get("paths") or {}).get(kind) or ""
    return v if v else default


def _eff_ffmpeg():
    """转码/截帧用的 ffmpeg 可执行文件（设置里可覆盖）。"""
    return effective_path("ffmpeg", FFMPEG)


def _eff_ffprobe():
    """探测视频编码用的 ffprobe（由 ffmpeg 路径派生，设置里改了 ffmpeg 就跟着变）。

    ⚠️ 只换**文件名**里的 ffmpeg：以前写的是 custom.replace("ffmpeg.exe","ffprobe.exe")
    .replace("ffmpeg","ffprobe")，后一个 replace 会把目录名
    （D:\\app\\ffmpeg-n8.1-...\\ffmpeg-n8.1-...）也换成 ffprobe，拼出一个不存在的路径
    → ffprobe 永远调不通 → 编码探测恒为空、转码永远按 720p 默认码率来（2026-11-02 修）。
    """
    custom = (load_settings().get("paths") or {}).get("ffmpeg") or ""
    if custom:
        d, base = os.path.split(custom)
        i = base.lower().find("ffmpeg")
        if i >= 0:
            base = base[:i] + "ffprobe" + base[i + len("ffmpeg"):]
        return os.path.join(d, base)
    return FFPROBE


# ============ 文件管理：文件夹改名 / 重复文件清理 ============
# 为什么放在服务端：改名要连带改 data.json 里的作品相对路径，删重复必须限定在隔离区目录内，
# 这两件事都不能交给前端直接碰磁盘。

FOLDER_NAME_RE = re.compile(r"^UID(\d+)_(.*)_发布作品$")
BAD_NAME_CHARS = '\\/:*?"<>|'
# 改名合并时挑出来的重复文件放这里（不在扫描范围内：目录名不以 _发布作品 结尾）
DUP_QUAR_DIRNAME = "_改名合并_重复文件_20261030"


def _dup_quar_root():
    return os.path.join(SCAN_ROOT, DUP_QUAR_DIRNAME)


def _name_problem(nick):
    """名字合法性检查（Windows 文件夹规则）。返回错误文案，合法返回空串。"""
    if not nick:
        return "名字不能为空"
    if len(nick) > 60:
        return "名字太长（最多 60 个字）"
    bad = sorted({c for c in nick if c in BAD_NAME_CHARS})
    if bad:
        return "名字里不能有这些字符：%s" % " ".join(bad)
    if nick in (".", ".."):
        return "名字不合法"
    # Windows 的 Win32 API 会把结尾的点和空格吃掉：`xxx.` 会变成 `xxx`，
    # 结果文件夹名和你填的不一样，重扫后还会被当成另一个名字。直接不让用。
    if nick.rstrip(" .") != nick:
        return "Windows 文件夹名不能以点（.）或空格结尾，请去掉结尾的这些字符"
    return ""


def _clean_nick(nick):
    """把昵称洗成「Windows 能当文件夹名」的样子：去掉非法字符 + 结尾的点/空格。"""
    n = (nick or "").strip()
    for c in BAD_NAME_CHARS:
        n = n.replace(c, "")
    return n.rstrip(" .")


def _files_rename_candidates():
    """列出「文件夹名 ≠ 你标注的昵称」的作者：这些人的文件夹还挂着旧名字。"""
    out = []
    for uid, a in (CACHE.get("accounts") or {}).items():
        folder = a.get("folder") or ""
        m = FOLDER_NAME_RE.match(folder)
        if not m:
            continue
        cur = m.group(2)
        nick = (a.get("nickname") or "").strip()
        if not nick or nick == cur:
            continue
        # 昵称里带 Windows 不让用的字符（`: > < *`、结尾的点）时，先洗成能建文件夹的样子再比：
        # 洗过之后 == 现在的文件夹名 → 已经是对的，跳过；洗过之后还能用 → 就按洗过的名字改。
        final = nick
        problem = _name_problem(final)
        if problem:
            alt = _clean_nick(nick)
            if alt == cur:
                continue
            if alt and not _name_problem(alt):
                final = alt
                problem = ""
        target = "UID%s_%s_发布作品" % (uid, final)
        out.append({
            "uid": uid,
            "nickname": nick,
            "final_nick": final,
            "alias": a.get("alias") or "",
            "folder": folder,
            "current": cur,
            "works": (a.get("stats") or {}).get("work_count") or 0,
            "target": target,
            "conflict": os.path.exists(os.path.join(SCAN_ROOT, target)),
            "problem": problem,
        })
    out.sort(key=lambda x: -x["works"])
    return out


def _patch_data_after_rename(data, uid, old_folder, new_folder):
    """把 data.json 里这个账号的文件夹名和作品相对路径一起改掉（不重扫也能立刻播）。"""
    acc = (data.get("accounts") or {}).get(uid)
    if not isinstance(acc, dict):
        return False
    acc["folder"] = new_folder
    acc["folder_rel"] = new_folder
    fs = [new_folder if f == old_folder else f for f in (acc.get("folders") or [])]
    acc["folders"] = fs or [new_folder]
    pref = old_folder + "/"
    hit = 0
    for w in acc.get("works") or []:
        for f in w.get("files") or []:
            rel = f.get("rel") or ""
            if rel.startswith(pref):
                f["rel"] = new_folder + "/" + rel[len(pref):]
                hit += 1
    # 封面引用的是「某个作品的某个文件」，文件夹改名后前缀也得跟着换——
    # 以前漏了这一步，结果 341 位作者改名后封面全变成深色空块（2026-10-30 修）。
    cov = acc.get("cover")
    if isinstance(cov, dict):
        wf = cov.get("workFile") or ""
        if wf.startswith(pref):
            cov["workFile"] = new_folder + "/" + wf[len(pref):]
            hit += 1
    return hit


def _patch_user_cover_after_rename(uid, old_folder, new_folder):
    """用户标注里也存了一份 cover（`load_user_data()` 会把它叠回索引），改名时一起换前缀。

    只改 data.json 不改这份，刷新后旧路径又会被盖回来，看上去就像「修了没用」。
    """
    try:
        ua = (_read_user_accounts() or {}).get(uid) or {}
        c = ua.get("cover")
        if not isinstance(c, dict):
            return
        wf = c.get("workFile") or ""
        pref = old_folder + "/"
        if wf.startswith(pref):
            merge_save_user_data({"accounts": {uid: {
                "cover": dict(c, workFile=new_folder + "/" + wf[len(pref):])}}})
    except Exception as e:
        print("[rename] 用户标注里的封面没同步:", e)


def _build_attr_tests(q):
    """按 query 构造「作者属性」判定函数：[(key, fn(item, acc), 'or'|'not'), ...]

    覆盖 等级 / 标签 / 账号状态 / 更新频率 / 已下载作品最后时间 / 是否有封面。
    每行自带一个开关（match_level=not 之类），语义按用户 2026-11-05 的口径：
        ＋（or）  = 包含：这一行勾中的值属于「我要的」
        －（not） = 排除：这一行勾中的值属于「我要去掉的」
    整体语义：先剔除所有 － 行命中的人，剩下的人里（存在 ＋ 行时）至少命中一个 ＋ 行。
    全是 ＋ 就是纯并集；全是 － 就是「全部作品，去掉这些」。默认 ＋。

    「导出作者信息」「新下载」「全部作者」三处共用这一份 —— 用户要求三边筛选一模一样。
    """
    def _one(name):
        return (q.get(name) or [""])[0]

    q_status = _one("status").strip()
    q_level = _one("level").strip()
    q_freq = _one("freq").strip()
    q_tag = _one("tag").strip()
    q_cover = _one("cover").strip()
    q_latest_after = _one("latest_after").strip()
    q_latest_before = _one("latest_before").strip()
    q_match = _one("match").strip().lower()

    def _attr_mode(key):
        v = (q.get("match_" + key) or [""])[0].strip().lower()
        if v:
            # 「and」是 2026-11-05 之前的旧叫法（交集），现在统一按「排除」处理，
            # 免得旧链接/旧书签点进来变成「必须同时满足」那种看不懂的结果
            return "not" if v in ("not", "and", "exclude") else "or"
        if q_match in ("not", "and"):
            return "not"
        return "or"

    # 状态/等级/频率/标签/封面都是逗号分隔多值；__unset = 未设置（字段为空），
    # __notag = 一个标签都没有（只在标签行有意义）
    status_set = {s.strip() for s in q_status.split(",") if s.strip()} if q_status else set()
    level_set = {s.strip() for s in q_level.split(",") if s.strip()} if q_level else set()
    freq_set = {s.strip() for s in q_freq.split(",") if s.strip()} if q_freq else set()
    tag_set = {s.strip() for s in q_tag.split(",") if s.strip()} if q_tag else set()
    cover_set = {s.strip().lower() for s in q_cover.split(",") if s.strip()} if q_cover else set()
    # 设置里勾了「不能设置更新频率」的状态（注销/禁言封号/卖号…）：这类账号不参与频率筛选
    try:
        freq_locked_status = {str(x).strip()
                              for x in (load_settings().get("update_freq_disabled_status") or [])}
    except Exception:
        freq_locked_status = set()

    # 统一做成判定函数列表，「未设置」用空串表示；__unset 只是候选值之一，
    # 不能把同组其它值一起吞掉（多选「未设置 + 注销」时注销那批不能消失）。
    tests = []
    if status_set:
        def _t_status(item, a, _s=status_set):
            st = (a or {}).get("status") or ""
            return (st == "" and "__unset" in _s) or (st in _s)
        tests.append(("status", _t_status))
    if level_set:
        def _t_level(item, a, _s=level_set):
            lv = (a or {}).get("level") or ""
            lv = LEVEL_ALIAS.get(lv, lv)
            return (lv == "" and "__unset" in _s) or (lv in _s)
        tests.append(("level", _t_level))
    if freq_set:
        def _t_freq(item, a, _s=freq_set, _lock=freq_locked_status):
            st = (a or {}).get("status") or ""
            if st in _lock:
                return False
            fr = (a or {}).get("update_freq") or ""
            return (fr == "" and "__unset" in _s) or (fr in _s)
        tests.append(("freq", _t_freq))
    if tag_set:
        def _t_tag(item, a, _s=tag_set):
            acc_tags = [str(t).strip() for t in ((a or {}).get("tags") or []) if str(t).strip()]
            # 「没有标签」：这条账号身上一个标签都没有（配合 － 圆钮就是「去掉所有带标签的」）
            if "__notag" in _s and not acc_tags:
                return True
            return any(t in acc_tags for t in _s if t != "__notag")
        tests.append(("tag", _t_tag))
    if cover_set:
        def _t_cover(item, a, _s=cover_set):
            has_cover = bool((a or {}).get("cover") or item.get("cover"))
            if "has" in _s and has_cover:
                return True
            if "none" in _s and not has_cover:
                return True
            return False
        tests.append(("cover", _t_cover))
    if q_latest_after or q_latest_before:
        def _t_latest(item, a, _a=q_latest_after, _b=q_latest_before):
            latest = (item.get("latest") or "")[:10]
            if _a and (not latest or latest < _a):
                return False
            if _b and (not latest or latest > _b):
                return False
            return True
        tests.append(("latest", _t_latest))

    return [(k, fn, _attr_mode(k)) for k, fn in tests]


def _attr_uids(q, accounts, lst):
    """属性筛选命中的 uid 集合；一个属性条件都没设时返回 None（= 不筛）。

    「新下载」页只关心「这些文件属于哪些作者」，筛选判定和 /api/authors 完全同源。
    """
    tests = _build_attr_tests(q)
    if not tests:
        return None
    out = set()
    for item in lst:
        a = accounts.get(item["uid"])
        # ＋ 行（or）：至少命中一个；－ 行（not）：命中就剔除。
        # 没有 ＋ 行时就是「全部作品，去掉 － 那几样」。
        excluded, or_any, has_or = False, False, False
        for _key, fn, mode in tests:
            hit = bool(fn(item, a))
            if mode == "not":
                if hit:
                    excluded = True
                    break
            else:
                has_or = True
                if hit:
                    or_any = True
        if not excluded and (not has_or or or_any):
            out.add(item["uid"])
    return out


def _files_rename_folder(uid, new_nick):
    uid = str(uid or "").strip()
    new_nick = (new_nick or "").strip()
    a = (CACHE.get("accounts") or {}).get(uid)
    if not isinstance(a, dict):
        return {"ok": False, "error": "找不到这个账号"}
    problem = _name_problem(new_nick)
    if problem:
        return {"ok": False, "error": problem}
    old_folder = a.get("folder") or ""
    m = FOLDER_NAME_RE.match(old_folder)
    if not m:
        return {"ok": False, "error": "原文件夹名不是标准格式：%s" % old_folder}
    new_folder = "UID%s_%s_发布作品" % (uid, new_nick)
    if new_folder == old_folder:
        return {"ok": True, "folder": old_folder, "message": "名字没变化"}
    src = os.path.join(SCAN_ROOT, old_folder)
    dst = os.path.join(SCAN_ROOT, new_folder)
    if not os.path.isdir(src):
        return {"ok": False, "error": "原文件夹不在磁盘上：%s" % old_folder}
    if os.path.exists(dst):
        return {"ok": False, "error": "已经存在同名文件夹：%s" % new_folder}
    try:
        os.rename(src, dst)
    except OSError as e:
        return {"ok": False, "error": "改名失败：%s" % e}

    # 下载记录库跟着改：这一步必须在 watcher 扫到新文件夹之前做完，
    # 否则它会按新路径另插一批，而旧路径的记录没有任何人删（file 表翻倍，文件数虚高）。
    try:
        download_db.rename_folder(old_folder, new_folder)
    except Exception as e:  # noqa: BLE001
        print("[rename] 下载记录库没同步:", e)

    # 同步索引。改不动就退回「后台重扫」，至少不会让页面长期指向死链。
    try:
        with _CACHE_LOCK:
            data = CACHE.get("data")
            if not isinstance(data, dict):
                raise RuntimeError("内存索引未载入")
            _patch_data_after_rename(data, uid, old_folder, new_folder)
            tmp = DATA_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(_index_only(data), fp, ensure_ascii=False)
            os.replace(tmp, DATA_JSON)
        load_data(force=True)
    except Exception as e:
        # 索引改不动（内存没载入 / 写盘失败）时不能假装成功：
        # 文件已经改名了，页面上的路径会指向不存在的文件 → 让用户点「⟳ 重新扫描」重建。
        return {"ok": True, "folder": new_folder, "need_rescan": True,
                "warning": "文件夹已改名，但索引没同步（%s）。请点顶栏「⟳ 重新扫描」重建索引。" % e}
    _patch_user_cover_after_rename(uid, old_folder, new_folder)
    return {"ok": True, "folder": new_folder, "message": "已改名并同步索引"}


# ===== 回收站：删作品不再直接抹掉文件 =====
# 用户要求「先放回收站」：删的时候把文件整份搬到 recycle-bin/<批次ID>/<作者文件夹>/<文件名>，
# 同时把被删作品的**完整 JSON** 记下来。所以「恢复」能把文件搬回原位、把作品条目插回索引，
# 效果和没删过一样。只有用户点「彻底删除」时才真的 rmtree。
RECYCLE_VIDEO_EXT = (".mp4", ".mov", ".webm", ".m4v")


def _sync_cache_after_change(uid, stats, acc=None):
    """改完 data.json 后别再整份读 74MB 回来。

    以前删一次作品要 load_data(force=True)：解析 74MB + 给 1702 个文件夹读 ctime，
    实测要 1.5~2 秒，用户点一下「删除」就干等在那儿。内存里这份本来就是刚写下去的
    同一份数据，只需要把作者列表里的统计数字对齐、mtime 跟上（否则下次请求又会重载）。

    ⚠️ 2026-10-31 补的坑：这里原来**只**同步 list_index 的三个统计字段，
    没同步 CACHE["accounts"][uid]（也就是 /api/author 读的那份）。
    结果：删/恢复作品后 CACHE["mtime"] 被设成新文件的 mtime，
    下次 load_data() 一看「mtime 相同」直接 return False，accounts 里永远是旧数据
    —— 表现成「详情页作品数一直是 0，但列表里有下载记录」，重启服务才好。
    现在把 acc 一起传进来同步；同时 mtime + size 双校验，光 mtime 不够可靠
    （原子替换 + 同一秒内多次写入时 mtime 可能相同）。
    """
    try:
        with _CACHE_LOCK:
            it = (CACHE.get("list_index") or {}).get(uid)
            if isinstance(it, dict) and isinstance(stats, dict):
                it["work_count"] = stats.get("work_count") or 0
                it["video_count"] = stats.get("video_count") or 0
                it["image_count"] = stats.get("image_count") or 0
                it["earliest"] = stats.get("earliest") or ""
                it["latest"] = stats.get("latest") or ""
            # accounts 里那份也一起对齐（/api/author 直接读它）
            if isinstance(acc, dict):
                cur = (CACHE.get("accounts") or {}).get(uid)
                if isinstance(cur, dict):
                    if isinstance(acc.get("works"), list):
                        cur["works"] = acc["works"]
                    if isinstance(acc.get("stats"), dict):
                        cur["stats"] = acc["stats"]
            CACHE["mtime"] = os.path.getmtime(DATA_JSON)
            try:
                CACHE["data_size"] = os.path.getsize(DATA_JSON)
            except OSError:
                pass
    except OSError:
        pass


def _recycle_load():
    """读 data/回收站.json（RECYCLE_JSON）；结构：{"batches": [{id, time, uid, nickname, works, idx, items, size}]}"""
    try:
        with open(RECYCLE_JSON, "r", encoding="utf-8") as fp:
            d = json.load(fp)
        if isinstance(d, dict) and isinstance(d.get("batches"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"batches": []}


def _recycle_save(d):
    """写回收站记录。返回是否成功。

    2026-11-06：以前失败只 print 就返回，调用方（删除作品）已经把所有文件搬进了
    recycle-bin/，结果「文件在、记录没了」—— 界面上看不到这批，既不能恢复也不能彻底删，
    只能自己去翻目录。现在返回布尔值，调用方据此整体回滚。
    """
    try:
        os.makedirs(os.path.dirname(RECYCLE_JSON), exist_ok=True)
        tmp = RECYCLE_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(d, fp, ensure_ascii=False)
        os.replace(tmp, RECYCLE_JSON)
        return True
    except OSError as e:
        print("[recycle] 记录写盘失败:", e, flush=True)
        return False


def _recycle_put(batch_id, rel, full):
    """把 full 搬进 recycle-bin/<batch_id>/<rel>，返回 (绝对路径, 批次内相对路径)。

    回收站内部也保留「作者文件夹/文件名」的结构，恢复时能一一对回去。

    ⚠️ 2026-11-06 改：**不再自动加 `_1` 后缀**。批次 ID 现在带随机尾巴（见
    `_works_delete_inner`），目录一定是新建的，同名只可能是同一作品里重复的 rel ——
    那是数据异常，宁可报错也绝不改名，否则记录里的 `rec` 和磁盘上的文件名对不上，
    恢复时就会「文件不见了」。
    """
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        raise OSError("非法的相对路径：%r" % (rel,))
    base_dir = os.path.join(RECYCLE_DIR, batch_id)
    dst = os.path.join(base_dir, *parts)
    if os.path.exists(dst):
        raise OSError("回收站里已经有同名文件：%s" % dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    # 同盘改名瞬时完成；跨盘（项目在 D:、媒体在 E:）时 shutil.move 会退化成
    # 复制 + 删除，这一步**必须在 _CACHE_LOCK 外做**，否则整个服务跟着卡住。
    shutil.move(full, dst)
    return dst, os.path.relpath(dst, base_dir).replace("\\", "/")


_BATCH_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,63}$")


def _recycle_batch_dir(batch_id):
    """批次目录。id 必须自己校验（2026-11-06 补）。

    原注释写着「只接受记录里存在的 id，杜绝传 ../.. 穿越」，但函数体只有一句 join ——
    校验全靠调用点先查记录。而 `data/回收站.json` 是普通文件，被别的脚本改出一个
    `"id": "../../.."` 就会让 rmtree 删到项目外。这里把口径落实到函数自己身上。
    非法 id 返回空串，调用点必须判空。
    """
    batch_id = str(batch_id or "").strip()
    if not batch_id or ".." in batch_id or not _BATCH_ID_RE.match(batch_id):
        return ""
    return os.path.join(RECYCLE_DIR, batch_id)


def _works_delete(uid, idxs, permanent=False):
    """删除某位作者的一批作品（按作品在 works 里的下标）。

    默认 **移入回收站**（可恢复）：文件搬到 recycle-bin/<批次ID>/ 下、作品条目从索引里摘掉、
    stats 跟着减，另写 data/删除记录.jsonl 留痕。
    permanent=True 才是真删（用户在回收站里点「彻底删除」走的是 _recycle_purge）。

    注意：回收站模式下**不清缩略图缓存**——恢复后 rel 没变，缓存还能直接用。
    """
    try:
        return _works_delete_inner(uid, idxs, permanent)
    except SystemExit:
        # 外部「批量删除保护」会抛 SystemExit（一次删超过阈值），不能让它把连接掐断
        return {"ok": False, "error": "删除被系统保护拦下（一次删的文件太多），已中止"}
    except BaseException as e:
        return {"ok": False, "error": "删除失败：%s" % e}


def _recycle_put_back(batch_id, items):
    """把已经搬进回收站的文件搬回原位（某个作品中途失败时的回滚）。返回搬回个数。

    跨盘时同样是复制 + 删除，但这条路只在「搬文件中途出错」时才走，属于罕见分支。
    """
    if not batch_id or not items:
        return 0
    base = os.path.join(RECYCLE_DIR, batch_id)
    n = 0
    for it in items:
        rec = str(it.get("rec") or "").replace("\\", "/")
        parts = [p for p in rec.split("/") if p not in ("", ".", "..")]
        dst = safe_media_path(it.get("rel") or "")
        if not parts or not dst:
            continue
        src = os.path.join(base, *parts)
        if not os.path.isfile(src):
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                shutil.move(src, dst)
                n += 1
        except OSError as e:
            print("[delete] 回滚失败 %s: %s" % (it.get("rel"), e), flush=True)
    return n


def _recycle_drop_empty(batch_id):
    """批次目录空了就把空壳删掉（回滚后 / 全部失败时不留空目录）。"""
    if not batch_id:
        return
    d = os.path.join(RECYCLE_DIR, batch_id)
    if not os.path.isdir(d):
        return
    try:
        for root, _dirs, files in os.walk(d, topdown=False):
            if not files:
                try:
                    os.rmdir(root)
                except OSError:
                    pass
    except OSError:
        pass


def _works_delete_inner(uid, idxs, permanent=False):
    """删除某位作者的一批作品（按作品在 works 里的下标）。

    默认 **移入回收站**（可恢复）：文件搬到 recycle-bin/<批次ID>/ 下、作品条目从索引里摘掉、
    stats 跟着减，另写 data/删除记录.jsonl 留痕。
    permanent=True 才是真删（用户在回收站里点「彻底删除」走的是 _recycle_purge）。

    2026-11-06 重构成三阶段（原来整段都在 _CACHE_LOCK 里）：
      ① 持锁只读：校验下标，把要删的作品 + 文件清单拷成「计划」，随即释放锁；
      ② 锁外搬文件：回收站在项目盘、媒体在 E:，跨盘时 shutil.move 是**真复制**，
         一批 15 GB 要几十秒 —— 这段时间以前会把所有 /api/* 全堵住（都要同一把锁）；
      ③ 重新持锁：按「文件名集合」把作品摘出索引、改 stats、写 data.json。

    另外两条不变式（原来没有）：
      · **作品是原子单位**：一个作品只要有任一文件搬不动，整条保留，并把它已经搬走的文件搬回原位；
      · **回收站记录先落盘再改索引**：记录写不进去就整体撤销，绝不留「文件在回收站、记录没了」的孤儿。
    """
    uid = str(uid or "").strip()
    if not isinstance(idxs, (list, tuple)) or not idxs:
        return {"ok": False, "error": "没有选中作品"}
    try:
        want = sorted({int(i) for i in idxs}, reverse=True)
    except (TypeError, ValueError):
        return {"ok": False, "error": "作品序号不合法"}
    if len(want) > 5000:
        return {"ok": False, "error": "一次最多删 5000 个作品"}

    t0 = time.time()

    # ---------- ① 持锁只读：做计划 ----------
    with _CACHE_LOCK:
        data = CACHE.get("data")
        if not isinstance(data, dict):
            return {"ok": False, "error": "索引未载入，请先点「⟳ 重新扫描」"}
        acc = (data.get("accounts") or {}).get(uid)
        if not isinstance(acc, dict):
            return {"ok": False, "error": "找不到这个账号"}
        works = acc.get("works") or []
        bad = [i for i in want if i < 0 or i >= len(works)]
        if bad:
            return {"ok": False, "error": "作品序号越界：%s" % bad[:3]}

        plan = []
        for i in want:
            w = works[i]
            files = []
            for f in (w.get("files") or []):
                rel = f.get("rel") or ""
                files.append({"rel": rel, "full": safe_media_path(rel)})
            plan.append({
                "idx": i,
                "work": json.loads(json.dumps(w, ensure_ascii=False)),
                # 作品指纹：文件名（rel）集合。阶段③用它重新定位作品，
                # 期间即使重扫过、下标全变了也能对上。
                "key": frozenset(x["rel"] for x in files),
                "files": files,
            })
        nickname = acc.get("nickname") or ""

    batch_id = ""
    if not permanent:
        import uuid
        # 秒级时间戳 + uid 后 6 位**不够唯一**：同一账号同一秒删两次会撞成同一批次，
        # 恢复第一批时 rmtree 会把第二批还在回收站里的文件一起删掉（2026-11-06 修）。
        batch_id = (time.strftime("%Y%m%d-%H%M%S") + "-" + (uid[-6:] or "0")
                    + "-" + uuid.uuid4().hex[:4])

    # ---------- ② 锁外搬文件（可能很慢） ----------
    failed = []
    results = []
    for p in plan:
        ok = True
        mv, rm, freed_this = [], [], 0
        for f in p["files"]:
            rel, full = f["rel"], f["full"]
            if not rel or not full:
                ok = False
                failed.append({"rel": rel, "error": "路径不合法"})
                continue
            try:
                if not os.path.isfile(full):
                    # 文件本来就不在了（上次删了一半 / 手工删的）：不算失败，
                    # 正好把这条僵尸作品从索引里清掉。
                    continue
                sz = os.path.getsize(full)
                if permanent:
                    os.remove(full)
                    rm.append(rel)
                else:
                    _dst, rec = _recycle_put(batch_id, rel, full)
                    mv.append({"rel": rel, "rec": rec, "size": sz})
                freed_this += sz
            except OSError as e:
                ok = False
                failed.append({"rel": rel, "error": str(e)})
        results.append({"plan": p, "ok": ok, "moved": mv,
                        "removed": rm, "freed": freed_this})

    # 回收站模式：单条作品失败 → 把它已搬走的文件搬回去，整条作品原样保留。
    # （真删模式删掉的文件没法回滚，只能把条目一起删掉并记进 failed）
    if not permanent:
        for r in results:
            if r["ok"] or not r["moved"]:
                continue
            back = _recycle_put_back(batch_id, r["moved"])
            print("[delete] 作品 %s 有文件搬不动，已回滚 %d 个文件"
                  % (r["plan"]["work"].get("desc") or "", back), flush=True)
            r["moved"] = []
            r["freed"] = 0

    done = results if permanent else [r for r in results if r["ok"]]
    if not done:
        if not permanent:
            _recycle_drop_empty(batch_id)
        return {"ok": False, "works": 0, "files": 0, "freed": 0, "failed": failed,
                "error": "选中的作品都有文件被占用，本次没有删除任何内容"}

    # ---------- ③ 记录先落盘，再改索引 ----------
    if not permanent:
        rec = _recycle_load()
        rec["batches"].insert(0, {
            "id": batch_id,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "uid": uid,
            "nickname": nickname,
            "works": [r["plan"]["work"] for r in done],
            "idx": [r["plan"]["idx"] for r in done],
            "items": [it for r in done for it in r["moved"]],
            "size": sum(it["size"] for r in done for it in r["moved"]),
        })
        if not _recycle_save(rec):
            # 记录写不进去 = 这批文件将来无法从界面恢复。整体撤销，一个文件都不删。
            back = 0
            for r in done:
                back += _recycle_put_back(batch_id, r["moved"])
            _recycle_drop_empty(batch_id)
            return {"ok": False, "works": 0, "files": 0, "freed": 0, "failed": failed,
                    "error": "回收站记录写入失败，本次删除已撤销（已把 %d 个文件搬回原位）" % back}

    # ---------- ④ 持锁改索引 ----------
    removed = []            # 留痕用
    v_del = i_del = 0
    with _CACHE_LOCK:
        data = CACHE.get("data")
        acc = (data.get("accounts") or {}).get(uid) if isinstance(data, dict) else None
        if not isinstance(acc, dict):
            return {"ok": False, "error": "账号已不在索引里（可能刚被重扫掉），"
                                          "文件已处理，请点「⟳ 重新扫描」重建索引"}
        del_keys = {r["plan"]["key"] for r in done}
        works = acc.get("works") or []
        keep = []
        for w in works:
            key = frozenset((f.get("rel") or "") for f in (w.get("files") or []))
            if key in del_keys:
                removed.append({
                    "date": w.get("date") or "",
                    "desc": (w.get("desc") or "")[:80],
                    "files": len(w.get("files") or []),
                })
                if any(os.path.splitext((f.get("rel") or ""))[1].lower() in RECYCLE_VIDEO_EXT
                       for f in (w.get("files") or [])):
                    v_del += 1
                else:
                    i_del += 1
                continue
            keep.append(w)
        acc["works"] = keep
        stats = acc.get("stats") or {}
        stats["work_count"] = max(0, int(stats.get("work_count") or 0) - len(removed))
        stats["video_count"] = max(0, int(stats.get("video_count") or 0) - v_del)
        stats["image_count"] = max(0, int(stats.get("image_count") or 0) - i_del)
        acc["stats"] = stats

        # 删掉的正好是「封面用的那个文件」→ 封面会 404，列表里变深色空块。
        # 顺手改指向他剩下的最新一条作品（文件还在的那种），别让删作品把别人的脸也删了。
        cover_new = None
        gone = {it["rel"] for r in results for it in r["moved"]}
        gone |= {rel for r in results for rel in r["removed"]}
        cov = acc.get("cover")
        if isinstance(cov, dict):
            wf = (cov.get("workFile") or "").replace("\\", "/")
            if wf and wf in gone:
                new_wf = ""
                for w in keep:
                    for f in (w.get("files") or []):
                        cand = f.get("rel") or ""
                        full2 = safe_media_path(cand) if cand else None
                        if full2 and os.path.isfile(full2):
                            new_wf = cand
                            break
                    if new_wf:
                        break
                cov["workFile"] = new_wf
                cover_new = dict(cov)

        try:
            tmp = DATA_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(_index_only(data), fp, ensure_ascii=False)
            os.replace(tmp, DATA_JSON)
        except OSError as e:
            return {"ok": False, "deleted": len(removed),
                    "files": sum(len(r["moved"]) + len(r["removed"]) for r in done),
                    "works": len(removed), "freed": sum(r["freed"] for r in done),
                    "failed": failed, "batch": "" if permanent else batch_id,
                    "error": "文件已处理，但索引写盘失败（%s）。请点「⟳ 重新扫描」重建。" % e}

    # data.json 改好了，用户标注里那份 cover 也要跟上（否则刷新后旧路径被叠回来）
    if cover_new is not None:
        try:
            merge_save_user_data({"accounts": {uid: {"cover": cover_new}}})
        except Exception as e:
            print("[delete] 用户标注里的封面没同步:", e, flush=True)

    # 真删模式才清缩略图缓存（按 rel 的 md5 命名）；回收站模式留着，恢复后直接能用
    removed_rels = [rel for r in results for rel in r["removed"]]
    for rel in removed_rels:
        try:
            tc = os.path.join(THUMB_CACHE, hashlib.md5(rel.encode("utf-8")).hexdigest() + ".jpg")
            if os.path.isfile(tc):
                os.remove(tc)
        except OSError:
            pass

    # 留痕：删了什么、什么时候删的，写进 jsonl，方便以后查
    try:
        log_path = os.path.join(os.path.dirname(DATA_JSON), "删除记录.jsonl")
        with open(log_path, "a", encoding="utf-8") as fp:
            fp.write(json.dumps({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "uid": uid,
                "nickname": nickname,
                "mode": "permanent" if permanent else "recycle",
                "batch": "" if permanent else batch_id,
                "works": removed,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass

    _sync_cache_after_change(uid, stats, acc)
    freed = sum(r["freed"] for r in done)
    files_n = sum(len(r["moved"]) + len(r["removed"]) for r in done)
    print("[delete] uid=%s works=%d files=%d freed=%.1fMB mode=%s %.2fs"
          % (uid, len(removed), files_n, freed / 1048576.0,
             "permanent" if permanent else "recycle", time.time() - t0), flush=True)
    return {"ok": True, "works": len(removed), "files": files_n,
            "freed": freed, "failed": failed, "batch": "" if permanent else batch_id,
            "mode": "permanent" if permanent else "recycle",
            "videos": v_del, "images": i_del}


def _recycle_list():
    """回收站里的批次概览（不含作品正文，只给数量和体积）。"""
    d = _recycle_load()
    out = []
    total = 0
    for b in d.get("batches") or []:
        size = int(b.get("size") or 0)
        total += size
        out.append({
            "id": b.get("id") or "",
            "time": b.get("time") or "",
            "uid": b.get("uid") or "",
            "nickname": b.get("nickname") or "",
            "works": len(b.get("works") or []),
            "files": len(b.get("items") or []),
            "size": size,
            "exists": os.path.isdir(_recycle_batch_dir(b.get("id") or "")),
        })
    return {"ok": True, "batches": out, "total_size": total, "dir": RECYCLE_DIR}


def _recycle_detail(batch_id):
    """回收站里某一批的**明细**：具体有哪些作品（日期 / 描述 / 文件数 / 文件名 / 体积）。

    2026-11-02 加：回收站列表只给「N 个作品」，用户点开才发现不知道删的是哪几条，
    全恢复又不甘心的情形。这里按批返回作品正文，前端点一行就展开看。
    体积按 items 里的 rel 对回 works 的文件名算，算不出来的给 0。
    """
    batch_id = str(batch_id or "").strip()
    d = _recycle_load()
    b = None
    for it in d.get("batches") or []:
        if it.get("id") == batch_id:
            b = it
            break
    if not b:
        return {"ok": False, "error": "回收站里没有这一批：%s" % batch_id}

    size_of = {}
    for it in b.get("items") or []:
        try:
            size_of[str(it.get("rel") or "").replace("\\", "/")] = int(it.get("size") or 0)
        except (TypeError, ValueError):
            pass

    works = []
    for w in b.get("works") or []:
        files = []
        wsize = 0
        for f in w.get("files") or []:
            rel = str(f.get("rel") or "")
            sz = size_of.get(rel.replace("\\", "/"), 0)
            wsize += sz
            files.append({"rel": rel, "name": os.path.basename(rel.replace("/", os.sep)), "size": sz})
        works.append({
            "date": w.get("date") or "",
            "desc": w.get("desc") or "",
            "media_type": w.get("media_type") or "",
            "files": files,
            "file_count": len(files),
            "size": wsize,
        })
    return {
        "ok": True,
        "id": b.get("id") or "",
        "time": b.get("time") or "",
        "uid": b.get("uid") or "",
        "nickname": b.get("nickname") or "",
        "works": works,
        "count": len(works),
        "size": int(b.get("size") or 0),
        "exists": os.path.isdir(_recycle_batch_dir(batch_id)),
    }


def _recycle_restore(batch_id):
    """把一批作品搬回原位、插回索引。文件先搬（不占锁），索引后改（占锁）。"""
    batch_id = str(batch_id or "").strip()
    d = _recycle_load()
    b = None
    for it in d.get("batches") or []:
        if it.get("id") == batch_id:
            b = it
            break
    if not b:
        return {"ok": False, "error": "回收站里没有这一批：%s" % batch_id}

    uid = b.get("uid") or ""
    bdir = _recycle_batch_dir(batch_id)
    if not bdir:
        return {"ok": False, "error": "批次 ID 不合法：%s" % batch_id}
    restored = 0
    failed = []
    for it in (b.get("items") or []):
        rel = it.get("rel") or ""
        dst = safe_media_path(rel)
        if not dst:
            failed.append({"rel": rel, "error": "路径不合法"})
            continue
        rec = (it.get("rec") or "").replace("\\", "/")
        src = os.path.join(bdir, *[p for p in rec.split("/") if p not in ("", ".", "..")])
        if not os.path.isfile(src):
            failed.append({"rel": rel, "error": "回收站里的文件不见了"})
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                # 原位已经有同名文件（用户后来又下回来了）：保留现有那份，删掉回收站副本
                os.remove(src)
            else:
                shutil.move(src, dst)
            restored += 1
        except OSError as e:
            failed.append({"rel": rel, "error": str(e)})

    with _CACHE_LOCK:
        data = CACHE.get("data")
        if not isinstance(data, dict):
            return {"ok": False, "error": "索引未载入，文件已搬回原位，请点「⟳ 重新扫描」"}
        acc = (data.get("accounts") or {}).get(uid)
        if not isinstance(acc, dict):
            return {"ok": False, "error": "这个账号已不在索引里（可能被重扫掉了），"
                                          "文件已搬回原位，请点「⟳ 重新扫描」重建"}
        works = acc.get("works") or []
        ws = b.get("works") or []
        idxs = b.get("idx") or []
        if len(idxs) != len(ws):
            idxs = [len(works) + k for k in range(len(ws))]
        pairs = sorted(zip(idxs, ws), key=lambda x: int(x[0]))
        # 期间如果重扫过 / 又下载回来了，索引里可能已经有这条作品——那就别再插一遍，
        # 否则列表里会同一作品出现两遍（文件已经按「原位有就不覆盖」处理过了）。
        exist_keys = {(w.get("date") or "", (w.get("desc") or "")[:60], len(w.get("files") or []))
                      for w in works}
        v_add = i_add = added = 0
        for k, w in pairs:
            key = (w.get("date") or "", (w.get("desc") or "")[:60], len(w.get("files") or []))
            if key in exist_keys:
                continue
            exist_keys.add(key)
            pos = max(0, min(int(k), len(works)))
            works.insert(pos, w)
            added += 1
            exts = [os.path.splitext((f.get("rel") or ""))[1].lower() for f in (w.get("files") or [])]
            if any(e in RECYCLE_VIDEO_EXT for e in exts):
                v_add += 1
            else:
                i_add += 1
        acc["works"] = works
        stats = acc.get("stats") or {}
        stats["work_count"] = int(stats.get("work_count") or 0) + added
        stats["video_count"] = int(stats.get("video_count") or 0) + v_add
        stats["image_count"] = int(stats.get("image_count") or 0) + i_add
        acc["stats"] = stats
        try:
            tmp = DATA_JSON + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(_index_only(data), fp, ensure_ascii=False)
            os.replace(tmp, DATA_JSON)
        except OSError as e:
            return {"ok": False, "error": "文件已搬回，但索引写盘失败（%s）。请点「⟳ 重新扫描」" % e}

    d["batches"] = [x for x in (d.get("batches") or []) if x.get("id") != batch_id]
    saved = _recycle_save(d)
    # 目录里可能还剩「没搬回去」的文件（failed 里的那些）——只删空目录，绝不 rmtree。
    # 之前这里是无条件 rmtree：恢复失败 = 文件被永久删掉，而界面上只提示「文件不见了」。
    _recycle_drop_empty(batch_id)
    _sync_cache_after_change(uid, stats, acc)
    res = {"ok": True, "works": len(ws), "files": restored, "failed": failed}
    if not saved:
        res["warning"] = "回收站记录写入失败，这一批可能仍显示在列表里（文件已搬回原位）"
    return res


def _recycle_purge(batch_id):
    """彻底删除一批（真删文件 + 丢掉记录）。不可恢复。"""
    batch_id = str(batch_id or "").strip()
    d = _recycle_load()
    b = None
    for it in d.get("batches") or []:
        if it.get("id") == batch_id:
            b = it
            break
    if not b:
        return {"ok": False, "error": "回收站里没有这一批：%s" % batch_id}
    bdir = _recycle_batch_dir(batch_id)
    if not bdir:
        return {"ok": False, "error": "批次 ID 不合法：%s" % batch_id}
    size = int(b.get("size") or 0)
    try:
        if os.path.isdir(bdir):
            shutil.rmtree(bdir)
    except OSError as e:
        return {"ok": False, "error": "删除失败：%s" % e}
    d["batches"] = [x for x in (d.get("batches") or []) if x.get("id") != batch_id]
    _recycle_save(d)
    return {"ok": True, "freed": size, "works": len(b.get("works") or [])}

def _recycle_purge_all():
    """一键清空回收站：所有批次真删（文件 + 记录），不可恢复。

    2026-11-02 加。一次删几万个文件要几十秒，单个批次就 15GB 的情况也遇到过，
    所以这里**逐批删、失败的单批留着继续删下一批**，最后把失败的报回去，
    不要因为一个目录被占用就整锅失败。
    """
    d = _recycle_load()
    batches = d.get("batches") or []
    if not batches:
        return {"ok": True, "freed": 0, "works": 0, "batches": 0, "failed": []}
    freed = 0
    works = 0
    kept = []
    failed = []
    for b in batches:
        bid = str(b.get("id") or "")
        bdir = _recycle_batch_dir(bid) if bid else ""
        try:
            if bdir and os.path.isdir(bdir):
                shutil.rmtree(bdir)
        except OSError as e:
            failed.append({"id": bid, "error": str(e)})
            kept.append(b)
            continue
        freed += int(b.get("size") or 0)
        works += len(b.get("works") or [])
    d["batches"] = kept
    _recycle_save(d)
    print("[recycle] purge_all batches=%d freed=%.1fMB failed=%d"
          % (len(batches) - len(kept), freed / 1048576.0, len(failed)))
    return {"ok": True, "freed": freed, "works": works,
            "batches": len(batches) - len(kept), "failed": failed}


def _files_rename_batch(uids=None, do_all=False):
    """批量把「文件夹名」改成「标注昵称」：一次改完只写一次 data.json、只重载一次索引。

    逐个改名每次都要整份写 70MB 的 data.json，改 300 个就是 300 次；批量走一遍就够。
    """
    cands = _files_rename_candidates()
    if do_all:
        targets = cands
    else:
        want = {str(u) for u in (uids or [])}
        targets = [c for c in cands if c["uid"] in want]
    if not targets:
        return {"ok": True, "renamed": 0, "failed": [], "message": "没有可改的"}
    done, failed = [], []
    with _CACHE_LOCK:
        data = CACHE.get("data")
        if not isinstance(data, dict):
            return {"ok": False, "error": "索引未载入，请先重新扫描"}
        for c in targets:
            if c["conflict"]:
                failed.append({"uid": c["uid"], "nickname": c["nickname"], "error": "已有同名文件夹"})
                continue
            if c["problem"]:
                failed.append({"uid": c["uid"], "nickname": c["nickname"], "error": c["problem"]})
                continue
            old = c["folder"]
            new = c["target"]
            src = os.path.join(SCAN_ROOT, old)
            dst = os.path.join(SCAN_ROOT, new)
            try:
                if not os.path.isdir(src):
                    raise RuntimeError("原文件夹不存在")
                if os.path.exists(dst):
                    raise RuntimeError("已有同名文件夹")
                os.rename(src, dst)
                _patch_data_after_rename(data, c["uid"], old, new)
                done.append({"uid": c["uid"], "nickname": c["nickname"],
                             "folder": new, "old": old})
            except Exception as e:
                failed.append({"uid": c["uid"], "nickname": c["nickname"], "error": str(e)})
        if done:
            try:
                tmp = DATA_JSON + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fp:
                    json.dump(_index_only(data), fp, ensure_ascii=False)
                os.replace(tmp, DATA_JSON)
            except Exception as e:
                return {"ok": True, "renamed": len(done), "failed": failed, "need_rescan": True,
                        "warning": "文件夹已改名，但索引没写成功（%s）。请点顶栏「⟳ 重新扫描」。" % e}
    if done:
        load_data(force=True)
        for it in done:
            _patch_user_cover_after_rename(it["uid"], it["old"], it["folder"])
    return {"ok": True, "renamed": len(done), "failed": failed,
            "message": "已改名 %d 个，失败 %d 个" % (len(done), len(failed))}


def _files_dup_stats():
    """隔离区（重复文件）按作者子目录汇总。"""
    root = _dup_quar_root()
    items = []
    tf = tb = 0
    if os.path.isdir(root):
        for name in sorted(os.listdir(root)):
            p = os.path.join(root, name)
            if not os.path.isdir(p):
                continue
            n = sz = 0
            for dirpath, _dirs, files in os.walk(p):
                for fn in files:
                    try:
                        sz += os.path.getsize(os.path.join(dirpath, fn))
                        n += 1
                    except OSError:
                        pass
            parts = name.split("_", 1)
            items.append({"name": name, "uid": parts[0],
                          "old": parts[1] if len(parts) > 1 else "",
                          "files": n, "bytes": sz})
            tf += n
            tb += sz
    items.sort(key=lambda x: -x["bytes"])
    return {"ok": True, "root": root, "items": items, "total_files": tf, "total_bytes": tb}


def _work_key_of(fn, nick):
    """把文件名归一成 (日期, 描述, 图序, 扩展名)：判断「是不是同一条作品」。

    文件名格式：`日期-昵称-描述[_序号].ext`。图序必须保留——多图作品的 `_1/_2`
    抹平后，一条图文会被误判成好几条重复。
    """
    m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.*)$", fn)
    if not m:
        return None
    date, body = m.group(1), m.group(2)
    pref = (nick or "") + "-"
    if nick and body.startswith(pref):
        body = body[len(pref):]
    else:
        parts = body.split("-", 1)
        if len(parts) > 1:
            body = parts[1]
    stem, ext = os.path.splitext(body)
    mm = re.search(r"_(\d+)$", stem)
    idx = mm.group(1) if mm else ""
    if mm:
        stem = stem[: mm.start()]
    return (date, stem, idx, ext.lower())


def _work_desc(fn, nick):
    """从文件名里取「描述」用于展示（去掉日期和昵称段）。"""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.*)$", fn)
    if not m:
        return os.path.splitext(fn)[0]
    body = m.group(2)
    pref = (nick or "") + "-"
    if nick and body.startswith(pref):
        body = body[len(pref):]
    else:
        parts = body.split("-", 1)
        body = parts[1] if len(parts) > 1 else body
    return os.path.splitext(body)[0]


def _safe_subdir(root, name):
    """在 root 下取一层子目录的绝对路径；名字非法/越界返回 None。

    越界判断用 realpath（不是 abspath）：目录里有 junction / 符号链接指向外部时，
    字符串前缀看起来「还在里面」，实际读写的是外面（2026-11-06 与 safe_media_path 口径统一）。
    """
    if not isinstance(name, str) or not name.strip():
        return None
    name = name.strip()
    if os.sep in name or "/" in name or name in (".", ".."):
        return None
    root_abs = os.path.abspath(root)
    p = os.path.abspath(os.path.join(root_abs, name))
    try:
        root_real = os.path.realpath(root_abs)
        if os.path.commonpath([root_real, os.path.realpath(p)]) != root_real:
            return None
    except (OSError, ValueError):
        return None
    return p


IMG_EXT = (".jpeg", ".jpg", ".png", ".webp", ".gif")


def _ver_entry(kind, fn, rel, size, mtime):
    ext = os.path.splitext(fn)[1].lower()
    return {"kind": kind, "file": fn, "rel": rel, "size": size,
            "mtime": int(mtime or 0), "ext": ext, "is_image": ext in IMG_EXT}


def _files_dup_group(name):
    """把某一组重复文件按「同一条作品」归并，返回每组的各个版本。

    一条作品正常有两版：`quarantine`（隔离区里的副本，文件名带旧昵称）和
    `current`（新文件夹里正在用的那份）。页面上把两版并排成气泡，让用户挑
    保留哪一份——所以这里必须把「在用那份」也当成一个版本返回，不能只给大小。
    """
    root = _dup_quar_root()
    gdir = _safe_subdir(root, name)
    if not gdir or not os.path.isdir(gdir):
        return {"ok": False, "error": "找不到这一组重复文件"}
    uid, _sep, old = name.partition("_")
    acc = (CACHE.get("accounts") or {}).get(uid) or {}
    tfolder = acc.get("folder") or ""
    nick = acc.get("nickname") or ""

    tmap = {}          # 作品键 -> (文件名, 大小, mtime)
    tpath = os.path.join(SCAN_ROOT, tfolder) if tfolder else ""
    if tpath and os.path.isdir(tpath):
        try:
            for fn in os.listdir(tpath):
                fp = os.path.join(tpath, fn)
                if not os.path.isfile(fp):
                    continue
                k = _work_key_of(fn, nick)
                if not k or k in tmap:
                    continue
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                tmap[k] = (fn, st.st_size, st.st_mtime)
        except OSError:
            pass

    groups = {}
    try:
        with os.scandir(gdir) as it:
            for e in it:
                try:
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat()
                except OSError:
                    continue
                k = _work_key_of(e.name, old)
                if not k:
                    continue
                g = groups.get(k)
                if g is None:
                    g = groups[k] = {
                        "key": list(k),
                        "date": e.name[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", e.name) else "",
                        "desc": _work_desc(e.name, old),
                        "versions": [],
                    }
                g["versions"].append(_ver_entry(
                    "quarantine", e.name, "%s/%s/%s" % (DUP_QUAR_DIRNAME, name, e.name),
                    st.st_size, st.st_mtime))
    except OSError as e:
        return {"ok": False, "error": "读目录失败：%s" % e}

    out = []
    for k, g in groups.items():
        hit = tmap.get(k)
        if hit:
            g["versions"].append(_ver_entry(
                "current", hit[0], "%s/%s" % (tfolder, hit[0]), hit[1], hit[2]))
            q = [v for v in g["versions"] if v["kind"] == "quarantine"]
            g["identical"] = all(v["size"] == hit[1] for v in q)
        else:
            g["identical"] = False
        g["versions"].sort(key=lambda v: (v["kind"] != "current", -v["size"]))
        g["has_current"] = bool(hit)
        out.append(g)
    out.sort(key=lambda g: (g["date"], g["desc"]), reverse=True)
    return {"ok": True, "name": name, "uid": uid, "old": old, "nick": nick,
            "target_folder": tfolder,
            "total_bytes": sum(v["size"] for g in out for v in g["versions"]
                               if v["kind"] == "quarantine"),
            "groups": out}


def _final_name_of(key, nick):
    """这条作品「应该叫什么」：日期-<当前昵称>-描述[_序号].扩展名。

    合并改名时只挪了文件夹和文件，文件名里的昵称还是当初下载时的旧名。
    用户要求「不管留哪份，名字都用现在最新的那个」，所以收尾时统一按这个拼。
    """
    date, stem, idx, ext = key
    suffix = "_" + idx if idx else ""
    return "%s-%s-%s%s%s" % (date, nick, stem, suffix, ext)


def _drop_thumb(rel):
    """删掉某条 rel 的缩略图缓存（缓存按 rel 的 md5 命名、不看 mtime）。"""
    try:
        tc = os.path.join(THUMB_CACHE, hashlib.md5(rel.encode("utf-8")).hexdigest() + ".jpg")
        if os.path.isfile(tc):
            os.remove(tc)
    except OSError:
        pass


def _files_dup_resolve(decisions):
    """按用户在前端的选择处理重复作品：每条作品保留一版，其余删掉。

    decisions: [{group, key:[日期,描述,图序,扩展名], keep:"current"|隔离区文件名}]
    - keep == "current"：保留新文件夹里那份，删掉隔离区副本
    - keep == 隔离区文件名：把那份搬进新文件夹，**并改名为最新的昵称**
    两种情况收尾都会把留下来那份**重命名为「日期-当前昵称-描述.ext」**，
    再把 data.json 里的 rel 一起改掉（不需要重扫）。

    安全：group 走 `_safe_subdir` 只认隔离区一层子目录；文件名必须在扫描到的
    目录列表里、且作品键对得上，绝不直接拼客户端给的路径。
    整个函数兜住 BaseException——删到一半被外部保护打断时，也要给前端一个
    正常的 JSON（否则浏览器只会看到 Failed to fetch）。
    """
    try:
        return _files_dup_resolve_inner(decisions)
    except BaseException as e:      # noqa: BLE001 —— 宁可返回错误也不能断连接
        return {"ok": False, "error": "处理中断：%s（已处理的部分不会回滚，"
                                      "刷新页面可以看到当前状态）" % e}


def _files_dup_resolve_inner(decisions):
    root = _dup_quar_root()
    if not os.path.isdir(root):
        return {"ok": False, "error": "隔离区目录不存在：%s" % root}

    by_group = {}
    for d in (decisions or []):
        if not isinstance(d, dict):
            continue
        g = d.get("group")
        if not isinstance(g, str) or not g.strip():
            return {"ok": False, "error": "组名不合法"}
        by_group.setdefault(g.strip(), []).append(d)

    deleted = restored = renamed = freed = 0
    failed = []
    warnings = []
    rel_pairs = {}          # 旧 rel -> 新 rel（用来一次性改 data.json）
    uids = set()
    aborted = None          # 被外部保护打断时的说明

    for gname, decs in by_group.items():
        if aborted:
            break
        gdir = _safe_subdir(root, gname)
        if not gdir or not os.path.isdir(gdir):
            failed.append({"group": gname, "error": "组不存在"})
            continue
        uid, _sep, old = gname.partition("_")
        acc = (CACHE.get("accounts") or {}).get(uid) or {}
        tfolder = acc.get("folder") or ""
        nick = acc.get("nickname") or ""
        tpath = os.path.join(SCAN_ROOT, tfolder) if tfolder else ""
        if not tfolder or not os.path.isdir(tpath):
            failed.append({"group": gname, "error": "找不到对应的作品文件夹"})
            continue
        uids.add(uid)

        # 文件名里用「文件夹名字里那段」，和已经改好的文件夹保持一致
        m = FOLDER_NAME_RE.match(tfolder)
        file_nick = _clean_nick(m.group(2) if m else nick) or _clean_nick(nick) or "未命名"

        qmap = {}
        try:
            for fn in os.listdir(gdir):
                fp = os.path.join(gdir, fn)
                if not os.path.isfile(fp):
                    continue
                k = _work_key_of(fn, old)
                if k:
                    qmap.setdefault(k, []).append(fn)
        except OSError as e:
            failed.append({"group": gname, "error": "读目录失败：%s" % e})
            continue
        tmap = {}
        try:
            for fn in os.listdir(tpath):
                if not os.path.isfile(os.path.join(tpath, fn)):
                    continue
                k = _work_key_of(fn, nick)
                if k and k not in tmap:
                    tmap[k] = fn
        except OSError:
            pass

        for d in decs:
            if aborted:
                break
            key = d.get("key")
            if not isinstance(key, (list, tuple)) or len(key) != 4:
                failed.append({"group": gname, "error": "作品键不合法"})
                continue
            k = tuple(str(x) for x in key)
            qfiles = qmap.get(k) or []
            if not qfiles:
                failed.append({"group": gname, "key": list(k), "error": "隔离区里已经没有这条作品"})
                continue
            keep = d.get("keep")
            want = _final_name_of(k, file_nick)
            dst = os.path.join(tpath, want)
            tfn = tmap.get(k)
            try:
                if keep == "current":
                    if not tfn:
                        failed.append({"group": gname, "key": list(k),
                                       "error": "没有正在用的那一份，不能只删副本"})
                        continue
                    src = os.path.join(tpath, tfn)
                    if tfn != want:
                        if os.path.exists(dst):
                            warnings.append("%s 已存在，跳过改名" % want)
                        else:
                            os.rename(src, dst)
                            rel_pairs["%s/%s" % (tfolder, tfn)] = "%s/%s" % (tfolder, want)
                            renamed += 1
                            tmap[k] = want
                            _drop_thumb("%s/%s" % (tfolder, want))
                    for fn in qfiles:
                        fp = os.path.join(gdir, fn)
                        sz = os.path.getsize(fp)
                        os.remove(fp)
                        deleted += 1
                        freed += sz
                else:
                    keep = str(keep or "")
                    if keep not in qfiles:
                        failed.append({"group": gname, "key": list(k),
                                       "error": "选中的文件不在这一组里"})
                        continue
                    if not tfn:
                        failed.append({"group": gname, "key": list(k),
                                       "error": "新文件夹里没有同一条作品，先不覆盖"})
                        continue
                    oldp = os.path.join(tpath, tfn)
                    if os.path.abspath(oldp) != os.path.abspath(dst) and os.path.isfile(oldp):
                        os.remove(oldp)          # 旧名那份让位
                    if tfn != want:
                        rel_pairs["%s/%s" % (tfolder, tfn)] = "%s/%s" % (tfolder, want)
                    os.replace(os.path.join(gdir, keep), dst)   # 搬进来并改成最新名字
                    restored += 1
                    if tfn != want:
                        renamed += 1
                    tmap[k] = want
                    _drop_thumb("%s/%s" % (tfolder, want))
                    for fn in qfiles:
                        if fn == keep:
                            continue
                        fp = os.path.join(gdir, fn)
                        sz = os.path.getsize(fp)
                        os.remove(fp)
                        deleted += 1
                        freed += sz
            except BaseException as e:      # 外部删除保护会抛 SystemExit
                msg = str(e) or e.__class__.__name__
                failed.append({"group": gname, "key": list(k), "error": msg})
                if isinstance(e, SystemExit) or "SAFE_DELETE" in msg.upper():
                    aborted = msg
                    break
        try:
            if not os.listdir(gdir):
                os.rmdir(gdir)
        except OSError:
            pass

    # 文件名改了 → 索引里的 rel 也得跟着改，否则点开是黑的
    if rel_pairs:
        try:
            with open(DATA_JSON, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            hit = 0
            for uid in uids:
                acc = (data.get("accounts") or {}).get(uid) or {}
                for w in acc.get("works") or []:
                    for f in w.get("files") or []:
                        r = f.get("rel")
                        if r in rel_pairs:
                            f["rel"] = rel_pairs[r]
                            hit += 1
            if hit:
                tmp = DATA_JSON + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fp:
                    json.dump(_index_only(data), fp, ensure_ascii=False)
                os.replace(tmp, DATA_JSON)
                load_data(force=True)
        except Exception as e:
            failed.append({"group": "", "error": "索引里的文件名没更新成功：%s（请点「⟳ 重新扫描」）" % e})

        # 封面引用的如果是旧文件名，也一起改
        try:
            cov = {}
            for uid, a in (_read_user_accounts() or {}).items():
                c = (a or {}).get("cover") or {}
                wf = c.get("workFile")
                if wf and wf in rel_pairs:
                    cov[uid] = {"cover": dict(c, workFile=rel_pairs[wf])}
            if cov:
                merge_save_user_data({"accounts": cov})
        except Exception as e:
            warnings.append("封面路径没同步：%s" % e)

    out = {"ok": True, "deleted": deleted, "restored": restored,
           "renamed": renamed, "freed": freed, "failed": failed,
           "warnings": warnings[:20]}
    if aborted:
        out["ok"] = False
        out["error"] = "删除被系统保护拦下（%s）。已处理 %d 份，其余没动。" % (aborted, deleted)
    return out


def _files_dup_delete(names=None, delete_all=False, group=None, files=None):
    """删隔离区里的东西：
    - `{names:[组目录]}` 删整组；`{all:true}` 删全部组
    - `{group, files:[文件名]}` 只删这一组里的指定作品（组空了就删目录）
    **只允许操作这个根目录下的一层子目录**，越界一律中止。
    """
    root = _dup_quar_root()
    if not os.path.isdir(root):
        return {"ok": False, "error": "隔离区目录不存在：%s" % root}
    root_abs = os.path.abspath(root)

    if group:
        gdir = _safe_subdir(root, group)
        if not gdir or not os.path.isdir(gdir):
            return {"ok": False, "error": "找不到这一组"}
        removed = 0
        freed = 0
        failed = []
        for fn in (files or []):
            if not isinstance(fn, str) or not fn.strip():
                return {"ok": False, "error": "文件名不合法"}
            fp = os.path.abspath(os.path.join(gdir, fn.strip()))
            try:
                inside = (os.path.commonpath([os.path.realpath(gdir), os.path.realpath(fp)])
                          == os.path.realpath(gdir))
            except (OSError, ValueError):
                inside = False
            if not inside:
                return {"ok": False, "error": "越界路径，已中止"}
            if not os.path.isfile(fp):
                continue
            try:
                sz = os.path.getsize(fp)
                os.remove(fp)
                removed += 1
                freed += sz
            except BaseException:
                failed.append(fn)
        try:
            if not os.listdir(gdir):
                os.rmdir(gdir)
        except OSError:
            pass
        return {"ok": True, "removed": removed, "freed": freed, "failed": failed}

    if delete_all:
        targets = [n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))]
    else:
        targets = []
        for n in (names or []):
            if not isinstance(n, str) or not n.strip():
                return {"ok": False, "error": "目录名不合法"}
            n = n.strip()
            if os.sep in n or "/" in n or n in (".", ".."):
                return {"ok": False, "error": "目录名里不能带路径：%s" % n}
            targets.append(n)
    removed = 0
    freed = 0
    failed = []
    for n in targets:
        p = os.path.abspath(os.path.join(root_abs, n))
        try:
            inside = (os.path.commonpath([os.path.realpath(root_abs), os.path.realpath(p)])
                      == os.path.realpath(root_abs))
        except (OSError, ValueError):
            inside = False
        if not inside:
            return {"ok": False, "error": "越界路径，已中止"}
        if not os.path.isdir(p):
            continue
        sz = 0
        for dirpath, _dirs, fs in os.walk(p):
            for fn in fs:
                try:
                    sz += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
        shutil.rmtree(p, ignore_errors=True)
        if os.path.exists(p):
            failed.append(n)
        else:
            removed += 1
            freed += sz
    return {"ok": True, "removed": removed, "freed": freed, "failed": failed}


SCRIPT_DIR = paths.SRC_DIR        # 代码目录（src/），只放代码
WEB_DIR = paths.WEB_DIR           # 前端三件套（src/web/）
USER_DATA = paths.USER_DATA       # 数据在 ../data/，见 paths.py
DATA_JSON = paths.DATA_JSON

# 数据库地址：设置里若指定了 data.json 所在目录，用它覆盖默认（重启服务后生效）。
# 仅影响 data.json 与 user_data.json 的读取位置；backup/导出等仍随项目目录走。
_data_dir = effective_path("data_dir", "")
if _data_dir and os.path.isdir(_data_dir):
    USER_DATA = os.path.join(_data_dir, "user_data.json")
    DATA_JSON = os.path.join(_data_dir, "data.json")

SCANNER_PY = paths.SCANNER_PY
PYTHON = paths.PYTHON
FFMPEG = paths.FFMPEG
FFPROBE = paths.FFPROBE
THUMB_CACHE = paths.THUMB_CACHE
TRANS_CACHE = paths.TRANS_CACHE   # 浏览器放不了的编码，转码缓存
# 封面图单独一个目录：**永不淘汰、永不清空**。用户明确要求「清缓存不要清我的封面缓存」。
# 以前封面借的是 thumb_cache（视频首帧），缓存到上限就会被当垃圾删掉，
# 下次打开再重新截帧——偶尔截帧失败/慢，看起来就像「我设的封面没了」。
COVER_CACHE = paths.COVER_CACHE
# 「抖音当前作品数」缓存：和标注分开放，避免把外部数据混进 user_data.json
COUNT_CACHE_PATH = paths.COUNT_CACHE_PATH
# 「作者文件夹创建时间」缓存（= 列表里「添加顺序」排序用的 added_at）。
# 见 _load_data_locked 里的注释：不缓存的话每次启动都要对 E: 机械盘做 1700 次 stat。
ADDED_AT_CACHE = os.path.join(paths.DATA_DIR, "added_at_cache.json")
# 抖音侧「最新发布时间 / 最后采集时间」（scanner 从下载器作品表 xlsx 里算出来的）
REMOTE_META_PATH = os.path.join(paths.DATA_DIR, "remote_latest.json")
# 联网查到的「抖音最新作品发布时间」（真实接口，见 latest_helper.py）
LATEST_CACHE_PATH = os.path.join(paths.DATA_DIR, "latest_work.json")
_COUNT_LOCK = threading.Lock()
COUNT_INTERVAL = 1.6          # 秒/个（1.2~2.0 随机抖动）：再快就容易撞风控
COUNT_CHUNK_MAX = 400         # 单轮最多刷多少个，避免一口气跑 1500 个
COUNT_STATE = {
    # 注意：不要用 "ok" 当计数键，接口返回体里的 "ok": true 会把它覆盖掉
    "running": False, "done": 0, "total": 0, "succeeded": 0, "failed": 0,
    "stop": False, "error": "", "started": "", "finished": "",
}
# 批量联网查「最新作品发布时间」：和数作品是两条完全不同的链路——
# 数作品走**公开接口**（不登录、不签名，风控压力小），发布时间必须走**登录态 + a_bogus
# 签名 + curl_cffi 指纹**打 aweme/post，单个请求重得多，所以节奏更慢、单轮更少、
# 连续失败更早熔断（8 个就停，而不是 20 个）。
_LATEST_STATE_LOCK = threading.Lock()
LATEST_INTERVAL = 3.5         # 秒/个（3.1~3.9 随机抖动）：对齐下载器 6 秒那档的保守路线
LATEST_CHUNK_MAX = 100        # 单轮最多查多少个
LATEST_PAUSE_EVERY = 25       # 每处理这么多个就歇一口气（下载器是每 10 个停 60 秒）
LATEST_PAUSE_SECS = 30        # 每次歇多久
LATEST_TTL_DAYS = 7           # 这个天数内查过就跳过（抖音删/设私密后要几天才稳定）
# 2026-10-31：详情页「打开就自动查」走的是 /api/latest_work，这里**必须有服务端缓存**。
# 前端虽然也记了 6 小时，但它只活在页面内存里（刷新就没了）；而「连着点开几十个作者」
# 正是最容易把请求打密的时候 —— 那道闸放在服务端才作数（用户选的是「只修缓存、不改节奏」）。
LATEST_DETAIL_TTL = 6 * 3600   # 同一个作者 6 小时内只联网查一次，其余直接回缓存
LATEST_FAIL_BREAK = 8         # 连续失败这么多个就停手
LATEST_STATE = {
    "running": False, "done": 0, "total": 0, "succeeded": 0, "failed": 0,
    "empty": 0,         # 对方没有公开作品（不是失败，不进熔断）
    "dead": 0,          # SEC_UID 已失效（账号注销/换号），要去重新补
    "skipped": 0, "stop": False, "error": "", "started": "", "finished": "",
    "pausing": False,   # 正在「每 25 个歇 30 秒」的空档里，前端显示 ⏸，免得看着像卡死
    "last_error": "",   # 最近一次失败的「昵称：原因」，跑完给用户看，省得只知道「失败了 N 个」
}
BACKUP_DIR = paths.BACKUP_DIR
# 标注台的落盘目录（2026-10-31）：用户在 /annotate.html 上圈出来的界面意见存这里，
# 文件名由服务端拼，前端传什么都不影响落盘位置。
ANNOTATE_DIR = paths.ANNOTATE_DIR
# 回收站：删作品 = 搬文件到这里（保留「作者文件夹/文件名」原结构），可原地搬回。
# 记录文件放数据目录，和 data.json 同处，方便一起备份。
RECYCLE_DIR = paths.RECYCLE_DIR
RECYCLE_JSON = os.path.join(os.path.dirname(DATA_JSON), "回收站.json")
AUTO_BACKUP_MIN_GAP = 180      # 秒：两次自动备份之间至少隔 3 分钟（以前 5 分钟，标注多时颗粒太粗）
AUTO_BACKUP_KEEP = 200         # 留最近 200 份自动备份（每份约 900KB，合计约 180MB；标注不可再生，宁可多留）
EXPORT_DIR = paths.EXPORT_DIR                            # SEC_UID 导出 txt 的存放目录
PYTHON312 = paths.PYTHON312                              # 下载器 curl_cffi 只支持 3.12
HELPER_QUERY_UID = paths.HELPER_QUERY_UID
HELPER_PROFILE = paths.HELPER_PROFILE   # 常驻：登录态查资料（3.12）
PROBE_CACHE = {}   # rel -> codec（内存缓存，避免重复 ffprobe）
BROWSER_OK_CODECS = {"h264", "vp8", "vp9", "av1", "mpeg4"}

# ============ data.json 写盘前的过滤（2026-11-01 修） ============
# 背景：data.json 只该有「扫描器产出的字段」。但 load_user_data() 是**原地 update**
# 进 CACHE["accounts"] 的，而 CACHE["accounts"] 的 value 就是 CACHE["data"]["accounts"]
# 的同一批 dict —— 于是内存里那份 CACHE["data"] 一直带着标注。
# 以前五个写盘点（改名单个 / 删作品 / 恢复回收站 / 批量改名 / 重复去重）都直接
# json.dump(CACHE["data"])，标注就跟着落了盘；而增量扫描是「读旧 data.json 再合并」，
# 写进去就再也洗不掉，还会变成过期残缺值（实测 starred 只剩 93，真值 105）。
#
# 修法：写盘一律过 _index_only()，把这些**扫描器永远不会写**的标注字段剥掉。
# 注意 douyin_id / signature / nickname / sec_uid / history 是扫描器 schema 里就有的字段，
# 值的来源可能被标注改过 —— 那是历史遗留的灰色地带，这里不动，免得丢 history。
ANNOT_ONLY_KEYS = frozenset((
    "alias", "alt_accounts", "cover", "level", "main_uid",
    "note", "starred", "status", "tags", "update_freq",
))


def _index_only(data):
    """返回只含扫描器字段的索引副本，供 data.json 写盘用。

    只浅拷贝「账号 -> 字段」这层字典：works / stats / history 里的内容仍是原对象引用，
    不复制几十 MB 的作品列表，所以开销可以忽略。
    内存里的 CACHE 不受影响（调用方读到的还是原来那份，标注照常生效）。
    """
    if not isinstance(data, dict):
        return data
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        return data
    clean = {}
    for uid, acc in accounts.items():
        if isinstance(acc, dict):
            clean[uid] = {k: v for k, v in acc.items() if k not in ANNOT_ONLY_KEYS}
        else:
            clean[uid] = acc
    out = dict(data)
    out["accounts"] = clean
    return out


# 全量扫描的真实进度（原来前端固定等 1.5 秒就刷新，扫几分钟时会让人以为没生效）
SCAN_STATE = {
    "running": False,
    "phase": "",      # 当前阶段：提取 sec_uid / 扫描作者文件夹 / 写入
    "done": 0,        # 已处理作者文件夹数
    "total": 0,       # 作者文件夹总数
    "started": "",
    "finished": "",
    "error": "",
    "auto": False,    # 这次是后台自动触发的增量扫描（不是用户点的重扫）
    "changes": 0,     # 后台检测到多少个作者文件夹有变化
}
_SCAN_LOCK = threading.Lock()
# 后台自动增量扫描。2026-10-31 改成**事件驱动**：
# 用 Windows 的 ReadDirectoryChangesW 挂在下载根目录上，DouK 一落盘系统就通知我们，
# 攒到「安静 5 秒」再跑一趟增量扫描（1~2 秒，只扫变了的文件夹）。
# 原来那个「每 5 分钟列一次根目录」的做法便宜（一次 scandir 0.09 秒）但它是**主动去问盘**——
# E: 机械盘本来能休眠，被这么定时问就一直醒着。现在没下载时一次 IO 都不发。
# 下面两个值只给「事件监听起不来」的兜底轮询用（老行为，保留着不至于没路可走）。
AUTO_SCAN_INTERVAL = 300
AUTO_SCAN_CHECK = 30      # 启动后先等这么久再开始查（避开服务启动高峰）
WATCHER = None            # 事件监听器（main() 里装；起不来就保持 None → 走兜底轮询）


def _folders_changed():
    """根目录 scandir 一次，和 data/scan_mtimes.json 里的 mtime 比，返回变化的文件夹名。

    这一步**不读文件内容**，只列一次根目录（Windows 的 DirEntry 自带 mtime，
    不额外读盘），所以哪怕每 5 分钟跑一次也很便宜。
    """
    try:
        import scanner as _sc
    except Exception:
        return []
    try:
        cur = _sc.scan_root_folders()
        old = _sc._load_mtimes()
    except Exception:
        return []
    if not cur:
        return []
    if set(cur) != set(old):
        return sorted(set(cur) ^ set(old))
    return [n for n, mt in cur.items() if old.get(n) != mt]


def scan_job(auto=False, changes=0):
    """跑一次扫描（scanner.py 自己是增量：只重扫变了的文件夹），把 PROGRESS/PHASE 转成进度。"""
    try:
        proc = subprocess.Popen(
            [PYTHON, SCANNER_PY], cwd=SCRIPT_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("PROGRESS "):
                parts = line[9:].split("/")
                try:
                    done, total = int(parts[0]), int(parts[1])
                except (ValueError, IndexError):
                    continue
                with _SCAN_LOCK:
                    SCAN_STATE["done"] = done
                    SCAN_STATE["total"] = total
            elif line.startswith("PHASE "):
                with _SCAN_LOCK:
                    SCAN_STATE["phase"] = line[6:].strip()
            elif ("复用" in line or "用时" in line or "错误" in line):
                # 增量扫描的结论行（「文件夹：复用 N / 重扫 M；作者 K 位」「完成，用时 X 秒」），
                # 打到服务日志里，下次「怎么又慢了」能直接看
                print("[scan] " + line, flush=True)
        code = proc.wait()
        if code != 0:
            with _SCAN_LOCK:
                SCAN_STATE["error"] = "扫描进程退出码 %d" % code
        else:
            load_data(force=True)
            with _SCAN_LOCK:
                SCAN_STATE["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
                SCAN_STATE["auto"] = bool(auto)
                SCAN_STATE["changes"] = int(changes or 0)
    except Exception as e:
        with _SCAN_LOCK:
            SCAN_STATE["error"] = str(e)
    finally:
        with _SCAN_LOCK:
            SCAN_STATE["running"] = False


def auto_scan_loop():
    """兜底：事件监听起不来时，退回「定时列一次根目录」的老办法。"""
    time.sleep(AUTO_SCAN_CHECK)
    while True:
        try:
            with _SCAN_LOCK:
                busy = SCAN_STATE["running"]
            if not busy:
                changed = _folders_changed()
                if changed:
                    _auto_scan_inner(len(changed), "轮询")
        except Exception as e:
            print("[auto] 循环异常：%s" % e, flush=True)
        time.sleep(AUTO_SCAN_INTERVAL)


def _auto_scan_inner(changes, why="事件"):
    """跑一趟后台增量扫描（调用方已确认当前没在扫）。"""
    with _SCAN_LOCK:
        SCAN_STATE.update(running=True, phase="后台增量扫描", done=0, total=0,
                          started=time.strftime("%Y-%m-%d %H:%M:%S"),
                          finished="", error="", auto=True, changes=int(changes or 0))
    print("[auto] %s：%d 个作者文件夹有新内容，增量扫描…" % (why, int(changes or 0)), flush=True)
    scan_job(auto=True, changes=int(changes or 0))
    with _SCAN_LOCK:
        fin, err = SCAN_STATE["finished"], SCAN_STATE["error"]
    print("[auto] 扫描%s（%s）" % ("失败：" + err if err else "完成", fin), flush=True)


def watch_loop():
    """事件驱动主循环：等系统通知 → 攒批 → 增量扫描。

    只在真有事的时候跑；没下载时它阻塞在 wait_change 上，不碰硬盘。
    """
    time.sleep(AUTO_SCAN_CHECK)

    # 服务没开的时候下载的东西不会产生事件（没人监听），所以启动后补查一次。
    # 这一步就是老办法：列一次根目录、比 mtime，0.09 秒，一次性。
    try:
        with _SCAN_LOCK:
            busy = SCAN_STATE["running"]
        if not busy:
            missed = _folders_changed()
            if missed:
                _auto_scan_inner(len(missed), "启动补查")
    except Exception as e:
        print("[watch] 启动补查失败：%s" % e, flush=True)

    quiet_rounds = 0
    while True:
        try:
            folders, unknown = WATCHER.wait_change(600)
        except Exception as e:
            print("[watch] 等待异常：%s" % e, flush=True)
            time.sleep(30)
            continue

        if not folders and not unknown:
            # 10 分钟没动静。顺手看一眼监听还活着没（盘掉线后 watcher 自己会重连）
            if WATCHER is not None and not WATCHER.alive:
                quiet_rounds += 1
                if quiet_rounds % 6 == 1:
                    print("[watch] 监听未就绪（%s），watcher 正在重连…"
                          % (WATCHER.error or "未知"), flush=True)
            continue

        # 上一趟还在扫（比如用户刚点了 ⟳）：等它跑完再扫，别两个进程抢着写 data.json
        waited = 0
        while waited < 600:
            with _SCAN_LOCK:
                if not SCAN_STATE["running"]:
                    break
            time.sleep(2)
            waited += 2
        with _SCAN_LOCK:
            if SCAN_STATE["running"]:
                print("[watch] 扫描一直没结束，这次事件丢掉，等下一批", flush=True)
                continue

        # unknown = 事件缓冲区溢出，知道有变化但不知道是哪个作者。
        # 交给 scanner 自己按 mtime 算（它本来就会算），这里的数字只用于弹提示。
        if unknown and not folders:
            try:
                folders = set(_folders_changed())
            except Exception:
                folders = set()
        _auto_scan_inner(len(folders), "事件")

# 批量获取资料：整批 1500 个账号要十几分钟，跑在前端就等于「页面不能关、面板不能关」。
# 改成服务端后台线程跑，前端点一下就能走，回来只看结果（状态放 BATCH_STATE，结果放 BATCH_RESULT）。
BATCH_STATE = {
    "running": False,
    "done": 0,          # 已查完的账号数
    "total": 0,
    "filled": 0,        # 补上空字段（抖音号 / 简介）的账号数
    "conflicts": 0,     # 昵称或抖音号跟本地不一致、留给用户决定的
    "failed": 0,        # 查不到的（账号注销 / 接口报错）
    "started": "",
    "finished": "",
    "error": "",
    "unread": False,    # 结果还没被前端取走 → 打开页面时自动弹出
    "stop": False,      # 用户点了「停止」
    "skipped": 0,       # 按「跳过状态」排除掉的账号数（不进 failed，只是没去查）
    "options": {},      # 本轮的设置（跳过状态/字段/顺序），前端回显用
}
BATCH_RESULT = {"conflicts": [], "failed": [], "filled": 0, "total": 0, "finished": ""}
_BATCH_LOCK = threading.Lock()

# 抓最近收藏/喜欢：后台线程跑，状态放 FAV_STATE（前端轮询 /api/favorites_status）
FAV_STATE = {"running": False, "done": 0, "total": 0, "result": None, "error": "", "source": "collect"}
_FAV_LOCK = threading.Lock()

# 批量获取资料的「获取内容」可选字段。顺序即前端展示顺序。
BATCH_FIELDS = ("nickname", "douyin_id", "signature")
BATCH_FIELD_LABEL = {"nickname": "昵称", "douyin_id": "抖音号", "signature": "简介"}
# 可以「跳过」的账号状态。禁言/封号/注销 默认跳（主页打不开或 404，查了也是白查，
# 还会连续失败触发限流保护、把整批拖停）。私密账号没被默认跳过：它的主页还在，
# 昵称/抖音号/简介都能取到，只是作品看不到。
BATCH_SKIPPABLE = ("禁言/封号", "注销", "私密账号")
BATCH_SKIP_DEFAULT = ("禁言/封号", "注销")
# 批量顺序：键 → (排序取什么, 方向)。added = 作者文件夹创建时间（什么时候下载进来的）
BATCH_ORDERS = {
    "added": ("added_at", True),        # 新下载的在前
    "added_asc": ("added_at", False),
    "work_count": ("work_count", True), # 作品多的在前
    "latest": ("latest", True),         # 最近更新的在前
    "earliest": ("earliest", False),    # 最早发布的在前
    "nickname": ("nickname", False),    # A→Z
}
# 结果落盘：服务重启后还能把「上次跑完的结果」交给前端（取走即删）
BATCH_RESULT_PATH = paths.BATCH_RESULT_PATH

# 转码任务：转码是同步阻塞的，长视频会让那次请求等很久 → 改成「起任务 + 轮询进度」
#   TRANS_JOBS[job_id] = {status: running|done|error, pct, url, rel, error, started}
#   TRANS_BY_REL[rel]  = job_id   （同一文件并发点开时复用同一个任务，别重复转）
TRANS_JOBS = {}
TRANS_BY_REL = {}
_TRANS_LOCK = threading.Lock()
TRANS_MAX_HEIGHT = 1080   # 转码目标最高 1080p（原来写死 720p，高清片子白白降质）

# ===== 缓存上限 =====
# thumb_cache / trans_cache 都是纯缓存（删了会按需重建），但不能无限长：
# 超了按「最久没被写过」淘汰。上限可以在「缓存」页面里一键清空。
# 2026-10-29：用户要求 图片（缩略图）3G、视频（转码）300M。封面不在缓存里，
# 走 COVER_CACHE，永不清理。
THUMB_CACHE_MAX_MB = 3072   # 缩略图（图片）3G
TRANS_CACHE_MAX_MB = 300    # 转码视频 300M
_CACHE_TRIM_LOCK = threading.Lock()   # 只保护缓存目录的增删，不要和 _CACHE_LOCK 混用
_THUMB_TRIM_TICK = [0]    # 每生成 N 张缩略图触发一次淘汰检查
THUMB_TRIM_EVERY = 200

# ---------- 硬件加速（2026-11-02）----------
# 分工原则：**显卡只干它真快的活**。
#   · 视频转码（HEVC→H.264，播放时才触发）：GPU 是**真快**，
#     15.7s 的源 openh264 32.8s → cuda 解码 + NVENC 8.7s，CPU 几乎不占 → 走 GPU。
#   · 抽封面（每个视频只取 1 帧）：GPU **更慢**。串行单张测着像"持平"，
#     并发 8 实测却是 26ms（CPU） vs 145ms（cuda），慢 5 倍多 → 走 CPU，见 _thumb_ffmpeg。
_NVENC = {"ok": None}       # None=还没试过 / True / False（失败一次就不再试）
# 同时跑几个 ffmpeg 抽封面：列表一次滚屏会并发问几十张，不限的话会同时起几十个
# ffmpeg（每个默认吃满所有核），互相抢核 → CPU 100% 且总耗时更长。
# 实测 24 个视频（每进程 2 线程）：并发 4 → 0.65s / 并发 8 → 0.62s / 并发 12 → 0.67s，
# 8 最优；默认线程数反而更慢（并发 8 时 1.19s），所以线程数必须压在 2。
_THUMB_SLOTS = threading.BoundedSemaphore(8)


def _nvenc_ok():
    """显卡编码能不能用（惰性，试过一次就记住结果）。"""
    return _NVENC["ok"] is not False


def _thumb_ffmpeg(full, out):
    """跑一次 ffmpeg 抽封面。**纯 CPU**，不碰显卡。

    2026-11-02 实测（24 个真实视频，并发 8）：
        CPU -threads 2  → 总墙钟 0.62s / 平均 26 ms   ← 现在走这条
        CPU 默认线程    → 总墙钟 1.19s / 平均 50 ms
        -hwaccel cuda   → 总墙钟 3.49s / 平均 145 ms
    显卡解码抽**单帧**反而慢 5 倍多：每个进程都要初始化一遍 NVDEC、再把这 1 帧
    从显存拷回来，这部分固定开销远大于「解码一帧」本身，并发时尤其明显
    （之前注释里写的「速度基本持平」是串行单张测的，被这个假象骗了）。
    显卡留给真正吃算力的活：HEVC→H.264 转码走 h264_nvenc，
    实测一段 15.7s 的源 32.8s → 8.7s，CPU 几乎不占。
    """
    # 图片不能 seek：一张 jpg 只有一帧，`-ss 1` 直接越过结尾 → 输出空文件 →
    # 前端拿到 500。照片墙（赏图模式）靠这个接口，一打开就是几百张，必须能出图。
    seek = [] if full.lower().endswith(IMG_EXT + (".bmp",)) else ["-ss", "1"]
    cmd = [_eff_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-nostats",
           "-threads", "2",            # 每进程只给 2 个线程：靠并发 8 个进程铺满核，别互相抢
           ] + seek + [
           "-i", full, "-frames:v", "1",
           "-vf", "scale=400:-2", "-q:v", "5", out]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        pass
    ok = os.path.isfile(out) and os.path.getsize(out) > 0
    if not ok:
        # 半截文件不能留在原地，否则外层 os.replace 会把破图当成品写进缓存
        try:
            if os.path.isfile(out):
                os.remove(out)
        except OSError:
            pass
    return ok

PORT = 8723

# 作者排序：默认方向（前端不传 dir 时用）。昵称/UID 正序更符合直觉，
# 数量/日期都是「大的在前」更好用。
SORT_DEFAULT_DIR = {
    "nickname": "asc", "uid": "asc",
    "work_count": "desc", "video_count": "desc", "image_count": "desc",
    "latest": "desc", "earliest": "desc", "level": "desc",
    "added": "desc",
    "download": "desc",     # 最近下过的在前（「谁很久没下载过」看升序）
}
# 等级排序权重：夯最高，未设置最低
# 旧值（1级（次）/2级（拉））保留在表里：备份恢复或老导出文件带回来时仍能排对，不会掉到最低
LEVEL_RANK = {"夯": 4, "顶级": 3, "一般": 2, "1级（次）": 2, "拉": 1, "2级（拉）": 1, "": 0}
# 2026-10-31 改名：1级（次）→一般、2级（拉）→拉。存量已迁移，这里兜底老backup/老导出带回来的旧值
LEVEL_ALIAS = {"1级（次）": "一般", "1级(次)": "一般", "2级（拉）": "拉", "2级(拉)": "拉"}

# 内存缓存：启动时载入 data.json（仅一次，Python 解析 62MB 很快）
CACHE = {
    "data": None,      # 完整 data.json
    "accounts": {},    # uid -> account（已叠加 user_data.json 的标注）
    "list": [],        # 作者列表元数据（不含 works）
    "list_index": {},  # uid -> 列表项。初始必须有：data.json 缺失时 load_data 会提前
                       # return，而 load_user_data 要往这里写合成账号，取不到就 KeyError
    "user_applied": {},  # 上一轮叠加过的标注，用来撤掉被用户删掉的字段
    "mtime": 0,        # data.json 修改时间，用于检测重扫
    "data_size": -1,   # data.json 字节数：和 mtime 一起校验（同一秒内多次替换 mtime 可能相同）
    "user_mtime": -1,  # user_data.json 修改时间，用于检测标注变化
    "synthetic_uids": [],  # 只在 user_data 里的合成账号（新建作者），重建列表时要先摘掉
    "data_uids": set(),    # 这一版 data.json 里真实存在的 uid。合成账号只在「不在这里面」时才重建，
                           # 否则作者下载完被扫描进来之后，会被当成合成账号重新建一遍 → 永远 0 作品
    "load_error": "",  # data.json 读坏了：接口要能告诉前端，而不是静默空列表
}


def _remote_meta_load():
    """{uid: {"pub": "YYYY-MM-DD", "crawl": "YYYY-MM-DD"}}；文件不在/坏了就空表。"""
    try:
        with open(REMOTE_META_PATH, "r", encoding="utf-8") as fp:
            d = json.load(fp)
    except (OSError, ValueError):
        return {}
    authors = d.get("authors") if isinstance(d, dict) else None
    return authors if isinstance(authors, dict) else {}


def _added_at_load():
    """读「文件夹 → 创建时间」缓存。坏了就当空表（下次启动全量 stat 一遍就自愈）。"""
    try:
        with open(ADDED_AT_CACHE, "r", encoding="utf-8") as fp:
            d = json.load(fp)
    except (OSError, ValueError):
        return {}
    folders = d.get("folders") if isinstance(d, dict) else None
    if not isinstance(folders, dict):
        return {}
    out = {}
    for k, v in folders.items():
        if isinstance(v, (int, float)) and v > 0:
            out[k] = float(v)
    return out


def _added_at_save(folders):
    """原子写回缓存（先写 .tmp 再 os.replace，写失败不影响启动）。"""
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        tmp = ADDED_AT_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"version": 1, "folders": folders}, fp, ensure_ascii=False)
        os.replace(tmp, ADDED_AT_CACHE)
    except OSError as e:
        print("[added_at] 缓存写入失败（不影响使用）：%s" % e)


def load_data(force=False):
    """载入/刷新 data.json 到内存。返回是否发生变化（重建全程持锁，见 _CACHE_LOCK）。"""
    with _CACHE_LOCK:
        return _load_data_locked(force)


def _load_data_locked(force=False):
    # 抖音侧发布时间：单独一个文件，data.json 没变也要跟着刷（DouK 跑完先动的是 xlsx）
    try:
        rl_mtime = os.path.getmtime(REMOTE_META_PATH)
    except OSError:
        rl_mtime = 0
    if force or CACHE.get("remote_mtime") != rl_mtime:
        CACHE["remote_meta"] = _remote_meta_load()
        CACHE["remote_mtime"] = rl_mtime
    try:
        mtime = os.path.getmtime(DATA_JSON)
    except OSError:
        return False
    # mtime + size 双校验：光看 mtime 不可靠 —— 扫描器是「写 tmp 再 os.replace」，
    # 同一秒内两次替换的 mtime 可能一样，而那两次的内容完全不同。
    # 只看 mtime 会出现「内存里是旧数据，但判定为已最新」的永久错位（踩过）。
    try:
        size = os.path.getsize(DATA_JSON)
    except OSError:
        size = -1
    if (not force and CACHE["data"] is not None
            and mtime == CACHE["mtime"] and size == CACHE.get("data_size")):
        return False
    try:
        with open(DATA_JSON, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError) as e:
        # 索引坏了不能把服务带崩（原来启动直接 traceback 退出），也不能把 CACHE 清空
        # （前端会显示 0 个作者，看起来像数据全丢）。保留上一次成功的内存索引，把错误挂出来。
        CACHE["load_error"] = "data.json 读取失败：%s" % e
        print("[data] " + CACHE["load_error"])
        return False
    if not isinstance(data, dict):
        CACHE["load_error"] = "data.json 结构异常（顶层不是对象）"
        print("[data] " + CACHE["load_error"])
        return False
    CACHE["load_error"] = ""
    accounts = data.get("accounts", {})
    # 有些老目录当初拿不到昵称、下载器把 sec_uid 当名字用，文件夹就成了
    # `UID123_MS4wLjAB…_发布作品`，扫描时原样取出来当昵称 → 列表上顶着一长串乱码
    # （2026-11-05 用户报的）。这里统一清洗内存副本：昵称看着像 sec_uid 就置空
    # （页面会退回显示 UID），顺便把 sec_uid 补上。磁盘索引交给 scanner 的新逻辑慢慢修。
    try:
        import scanner as _sc
        for _uid_c, _acc_c in accounts.items():
            if not isinstance(_acc_c, dict):
                continue
            _nk = _acc_c.get("nickname") or ""
            if _sc.looks_like_sec_uid(_nk):
                _acc_c["nickname"] = ""
                if not (_acc_c.get("sec_uid") or "").strip():
                    _acc_c["sec_uid"] = _nk.strip()
    except Exception as _e:  # noqa: BLE001
        print("[data] 昵称清洗跳过：%s" % _e)
    # 记下「这一版索引里到底有哪些 uid」（黑名单剔除前）。load_user_data 靠它区分
    # 「还是合成账号」和「已经被扫描进来了」—— 见 _load_user_data_locked 里那段说明。
    CACHE["data_uids"] = set(accounts.keys())
    # 黑名单过滤：命中「不下载名单」的作者直接从内存索引里剔除，
    # 主页列表、搜索、侧边栏统计都不会再出现（磁盘文件原样保留，重扫也不会放回来）。
    # 用 uid / sec_uid / 昵称三路匹配，改名也能兜底。
    _bl = _BL.get()
    # 构建轻量作者列表（不含 works，避免一次性传几十 MB）
    lst = []
    blocked = []
    # 「添加顺序」= 作者文件夹的创建时间。**这一步原来每次启动都对 E: 盘做 1700 次
    # getctime**——E: 是 3.7T 机械盘，盘刚唤醒时这一串 stat 能磨到 30 秒以上，
    # 窗口就停在「正在载入索引...」不动（2026-10-30 用户报「怎么这么慢」）。
    # ctime 一旦建好就不会变，所以落盘缓存：命中直接取值，盘只在出现**新文件夹**时
    # （新下载的作者、改名后的新路径）才读一次。
    added_map = _added_at_load()
    added_seen = set()
    stat_n = 0
    hit_n = 0
    t_stat = time.time()
    # 「这个作者多久没下载过」：来自我们自己的 data/download_log.db（扫描时按文件 mtime 记的）
    try:
        dl_map = download_db.summary()
    except Exception as e:
        print("[download_db] 读取失败（不影响列表）：%s" % e)
        dl_map = {}
    _rl = CACHE.get("remote_meta") or {}
    for uid, a in accounts.items():
        if _bl.is_blocked(sec_uid=(a.get("sec_uid") or ""), uid=uid,
                          nickname=(a.get("nickname") or "")):
            blocked.append(uid)
            continue
        fr = a.get("folder_rel") or a.get("folder") or ""
        added_at = None
        if fr:
            if fr in added_map:
                added_at = added_map[fr]
                hit_n += 1
            else:
                try:
                    added_at = int(os.path.getctime(os.path.join(SCAN_ROOT, fr.replace("/", os.sep))))
                except OSError:
                    added_at = 0      # 读不到就 fallback 0，且不写进缓存（下次启动再试一次）
                else:
                    added_map[fr] = added_at
                stat_n += 1
        added_seen.add(fr)
        lst.append({
            "uid": uid,
            "nickname": a.get("nickname", ""),
            "sec_uid": a.get("sec_uid", ""),
            "douyin_id": a.get("douyin_id", ""),
            "work_count": (a.get("stats") or {}).get("work_count", 0),
            "video_count": (a.get("stats") or {}).get("video_count", 0),
            "image_count": (a.get("stats") or {}).get("image_count", 0),
            "earliest": (a.get("stats") or {}).get("earliest", ""),
            "latest": (a.get("stats") or {}).get("latest", ""),
            "cover": a.get("cover") or None,
            "added_at": added_at or 0,
            # 下载记录：最后一次下载时间（文件写入时间）/ 已记文件数
            "last_download": (dl_map.get(uid) or {}).get("last") or 0,
            "download_count": (dl_map.get(uid) or {}).get("count") or 0,
            # 抖音侧：最新发布时间（DouK 最近一次抓到的）+ 那次采集时间
            "remote_latest": (_rl.get(uid) or {}).get("pub") or "",
            "remote_crawl": (_rl.get(uid) or {}).get("crawl") or "",
        })
    # 缓存只留还在索引里的文件夹，删号/改名留下的旧键清掉，文件不会越滚越大
    if added_map:
        pruned = {k: v for k, v in added_map.items() if k in added_seen}
    else:
        pruned = {}
    if stat_n or len(pruned) != len(added_map) or not os.path.isfile(ADDED_AT_CACHE):
        _added_at_save(pruned)
    CACHE["load_stat"] = {"hit": hit_n, "stat": stat_n, "sec": round(time.time() - t_stat, 2)}
    # 被拉黑的作者要连 accounts dict 一起剔除，否则 /api/meta 的 total_authors、
    # 星标/标签统计会多算，load_user_data 也会把它们的标注再叠加回来。
    for uid in blocked:
        accounts.pop(uid, None)
    CACHE["data"] = data
    CACHE["accounts"] = accounts
    CACHE["list"] = lst
    CACHE["list_index"] = {it["uid"]: it for it in lst}
    CACHE["mtime"] = mtime
    CACHE["data_size"] = size
    # 索引重建后必须重新叠加用户标注，否则星标/标签/状态全丢
    CACHE["user_mtime"] = -1
    load_user_data(force=True)
    return True


def load_user_data(force=False):
    """把 user_data.json 的标注叠加到内存里的 accounts（只读，不写回 data.json）。

    没有这一步的话，/api/meta 的星标数、标签云、/api/authors 的 cat=starred
    都会读 data.json 里根本不存在的 starred/tags 字段，永远为空。

    并发约定：重建全程持 _CACHE_LOCK，且**只替换 CACHE 里的引用、不原地 append**，
    这样别的线程即使已拿到旧列表在遍历，也不会撞上
    「dictionary changed size during iteration」。
    """
    with _CACHE_LOCK:
        return _load_user_data_locked(force)


def _load_user_data_locked(force=False):
    try:
        mtime = os.path.getmtime(USER_DATA)
    except OSError:
        return False
    if not force and mtime == CACHE.get("user_mtime"):
        return False
    try:
        with open(USER_DATA, "r", encoding="utf-8") as fp:
            ud = json.load(fp)
    except (OSError, ValueError):
        return False
    # 结构自愈（与 merge_save_user_data 同一套）：读到顶层化的坏结构就包回 accounts，
    # 否则这里的 new_acc 会变成空 dict，表现为「全部标注消失」
    if "accounts" not in ud and isinstance(ud, dict) and ud:
        ud = {"accounts": ud}
    new_acc = ud.get("accounts") or {}

    # 全部改在副本上做，最后一次性替换引用
    accounts = dict(CACHE.get("accounts") or {})
    lst = list(CACHE.get("list") or [])
    list_index = dict(CACHE.get("list_index") or {})

    # 上一轮的「合成账号」（只在 user_data 里、data.json 没有的，比如新建作者）先摘掉，
    # 下面按当前标注重建——否则改名/删除后列表里残留旧值。
    #
    # ⚠️ 2026-11-01 的坑：这里原来**无条件** pop，不检查这一版 data.json 里是不是已经有他了。
    # 于是作者从「新建作者」变成「下载完了、被扫描进 data.json」之后，每次刷新标注都会把
    # 刚读进来的真账号 pop 掉、再按 user_data 重新建一个空壳 → 作品数永远 0、主页永远
    # 「该分类下暂无作品」，只有重启进程（synthetic_uids 被清空）才好。
    # 现在只摘「这一版 data.json 里确实没有」的那些。
    old_synthetic = set(CACHE.get("synthetic_uids") or [])
    data_uids = CACHE.get("data_uids") or set()
    stale_synthetic = {u for u in old_synthetic if u not in data_uids}
    for uid in stale_synthetic:
        accounts.pop(uid, None)
        list_index.pop(uid, None)
    if stale_synthetic:
        lst = [it for it in lst if it.get("uid") not in stale_synthetic]
    synthetic = []
    # 用户删掉某个标注字段时，要把它从内存里撤掉，否则旧值会一直残留
    for uid, old in (CACHE.get("user_applied") or {}).items():
        acc = accounts.get(uid)
        if not isinstance(acc, dict) or not isinstance(old, dict):
            # 非 dict 的历史快照（user_data 里某条被写成 null / 数字）没法表达
            # 「哪些字段被删过」，跳过即可；不加这个判断会 `for k in None` 抛 TypeError，
            # 整个列表接口变成空响应（2026-11-06）
            continue
        cur = new_acc.get(uid)
        if not isinstance(cur, dict):
            cur = {}
        for k in old:
            if k not in cur:
                acc.pop(k, None)
    for uid, ua in new_acc.items():
        acc = accounts.get(uid)
        if isinstance(acc, dict) and isinstance(ua, dict):
            acc.update(ua)
            # 列表卡片直接用 lst 里的值，改过的昵称/抖音号要同步，否则搜索和列表还是旧值
            it = list_index.get(uid)
            if it:
                for k in ("nickname", "douyin_id", "sec_uid", "cover", "alias"):
                    if k in ua:
                        it[k] = ua[k]
    # 合成账号：user_data 里有标注但 data.json 没有的 uid（新建作者/手工填的 UID）。
    # 补进 accounts 和作者列表，让它们能出现在「全部作者」、能被搜索、能被批量获取资料、
    # 能被导出——作品数为 0，卡片上显示「无本地作品」。
    for uid, ua in new_acc.items():
        if uid in accounts or not isinstance(ua, dict):
            continue
        # 黑名单作者即便在 user_data 里有标注，也不能作为「合成账号」补回来，
        # 否则 load_data 里刚把它们从 data.json 剔除，这里又给塞回列表了。
        if _BL.get().is_blocked(sec_uid=(ua.get("sec_uid") or ""), uid=uid,
                                nickname=(ua.get("nickname") or "")):
            continue
        entry = {
            "uid": uid,
            "nickname": ua.get("nickname", "") or "",
            "sec_uid": ua.get("sec_uid", "") or "",
            "douyin_id": ua.get("douyin_id", "") or "",
            "alias": ua.get("alias", "") or "",
            "work_count": 0, "video_count": 0, "image_count": 0,
            "earliest": "", "latest": "",
            "cover": ua.get("cover") or None,
            "added_at": 0,          # 合成账号没有本地文件夹，没有创建时间
        }
        acc = dict(entry)
        acc.update(ua)
        accounts[uid] = acc
        lst.append(entry)
        list_index[uid] = entry
        synthetic.append(uid)

    CACHE["accounts"] = accounts
    CACHE["list"] = lst
    CACHE["list_index"] = list_index
    CACHE["synthetic_uids"] = synthetic
    CACHE["user_mtime"] = mtime
    # 存一份浅拷贝，别把磁盘 JSON 的 dict 直接挂进缓存：那份会被 acc.update() 之后
    # 的原地修改带上（删字段的比较基准就不准了）
    CACHE["user_applied"] = {u: dict(v) for u, v in new_acc.items() if isinstance(v, dict)}
    return True


def normalize_alt_groups(accounts):
    """把 alt_accounts 归一化成「组内全连」：同一组的所有成员互相关联。

    数据结构：accounts[uid]["alt_accounts"] = [同组其他所有人的 uid]（不含自己）
    不变量：x ∈ alt[y] ⇔ y ∈ alt[x]，且同组任意两人看到的其他成员集合完全相同。

    老逻辑只写「两端」，会出现「A 关联了 B、C，但 B 只知道 A」。这里用并查集求
    连通分量，再把每个分量重写成团，顺带修掉空数组等脏数据。
    幂等：重复调用不会再有变化。返回被修正的条目数。
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for uid, ua in accounts.items():
        find(uid)
        if not isinstance(ua, dict):
            continue
        arr = ua.get("alt_accounts")
        if not isinstance(arr, list):
            continue
        for v in arr:
            if isinstance(v, str) and v and v != uid:
                union(uid, v)

    groups = {}
    for u in list(parent.keys()):
        groups.setdefault(find(u), []).append(u)

    changed = 0
    for members in groups.values():
        if len(members) < 2:
            ua = accounts.get(members[0])
            if isinstance(ua, dict) and isinstance(ua.get("alt_accounts"), list) and not ua["alt_accounts"]:
                ua.pop("alt_accounts", None)
                changed += 1
            continue
        ms = sorted(members)
        for u in ms:
            others = [x for x in ms if x != u]
            ua = accounts.get(u)
            if not isinstance(ua, dict):
                ua = accounts[u] = {}
            cur = ua.get("alt_accounts")
            if not isinstance(cur, list) or sorted(cur) != others:
                ua["alt_accounts"] = others
                changed += 1
    return changed


VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def is_video_rel(rel):
    return os.path.splitext(str(rel or ""))[1].lower() in VIDEO_EXTS


def cover_thumb_name(rel):
    """封面缓存文件名（和 thumb_cache 同名规则，便于老数据一次性搬过来）。"""
    return hashlib.md5(str(rel).encode("utf-8")).hexdigest() + ".jpg"


def _read_user_accounts(path=None):
    try:
        with open(path or USER_DATA, "r", encoding="utf-8") as fp:
            d = json.load(fp)
        return d.get("accounts") or {}
    except (OSError, ValueError):
        return {}


def cover_cache_names():
    """当前所有「视频封面」对应的缓存文件名 —— 淘汰缩略图时要留它们一命。"""
    names = set()
    for a in _read_user_accounts().values():
        if not isinstance(a, dict):
            continue
        c = a.get("cover")
        if isinstance(c, dict) and not c.get("custom") and is_video_rel(c.get("workFile")):
            names.add(cover_thumb_name(c["workFile"]))
    return names


def migrate_cover_cache():
    """启动时把已有封面从 thumb_cache 搬进 cover_cache（一次性，纯本地拷贝）。"""
    os.makedirs(COVER_CACHE, exist_ok=True)
    copied = 0
    for name in cover_cache_names():
        dst = os.path.join(COVER_CACHE, name)
        if os.path.isfile(dst):
            continue
        src = os.path.join(THUMB_CACHE, name)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, dst)
                copied += 1
            except OSError:
                pass
    if copied:
        print(f"封面已搬到独立目录: {copied} 张（{COVER_CACHE}）")
    return copied


def _auto_backup():
    """写 user_data.json 之前先快照一份，最多 5 分钟一份、留最近 40 份。

    目的：任何一次误删/误覆盖都能退回到几分钟前，而不是只能翻很久以前的 .bak。
    """
    try:
        if not os.path.isfile(USER_DATA):
            return
        os.makedirs(BACKUP_DIR, exist_ok=True)
        pattern = os.path.join(BACKUP_DIR, "user_data.auto-*.json")
        olds = sorted(glob.glob(pattern), key=os.path.getmtime)
        if olds and time.time() - os.path.getmtime(olds[-1]) < AUTO_BACKUP_MIN_GAP:
            return
        dst = os.path.join(BACKUP_DIR, time.strftime("user_data.auto-%Y%m%d-%H%M%S.json"))
        shutil.copy2(USER_DATA, dst)
        olds = sorted(glob.glob(pattern), key=os.path.getmtime)
        for f in olds[:-AUTO_BACKUP_KEEP]:
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception:
        pass          # 备份失败绝不能挡住正常保存


def _count_cache_load():
    try:
        with open(COUNT_CACHE_PATH, "r", encoding="utf-8") as fp:
            d = json.load(fp)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


_COUNT_CACHE = _count_cache_load()


def _count_cache_save():
    """落盘作品数缓存。

    先 dumps 成字符串（在锁内取快照）再写文件：json.dump 直接迭代共享 dict 时，
    另一线程插入新 key 会抛 RuntimeError: dictionary changed size during iteration，
    而旧代码只 catch OSError，异常会一路冒到接口里。临时文件也必须唯一命名，
    否则两个线程同时写同一个 .tmp 会互相截断，留下半截 JSON。
    """
    import tempfile
    try:
        with _COUNT_LOCK:
            text = json.dumps(_COUNT_CACHE, ensure_ascii=False)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(COUNT_CACHE_PATH),
                                   prefix=".count_cache.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fp:
                fp.write(text)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, COUNT_CACHE_PATH)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, ValueError, RuntimeError):
        pass


def fetch_aweme_count(uid, sec_uid=""):
    """读抖音上的「当前作品数」。

    优先用**不需要登录**的公开接口（不消耗账号风控额度，也不会牵连同一个人登录态）；
    失败再降级到登录态接口。返回 (数量, 来源) 或 (None, 错误说明)。

    注意口径：`aweme_count` 是抖音对外可见的作品数，私密/仅好友可见的不算，
    所以它可能比本地下载的数量少——这本身是有用信息（能看出对方删了多少/设私密多少）。
    """
    uid = str(uid or "").strip()
    sec_uid = str(sec_uid or "").strip()
    if sec_uid:
        import urllib.request
        url = ("https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid="
               + urllib.parse.quote(sec_uid))
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
            "Referer": "https://www.douyin.com/",
        })
        try:
            raw = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace")
            d = json.loads(raw)
            if d.get("status_code") == 0:
                ui = d.get("user_info") or {}
                cnt = ui.get("aweme_count")
                if isinstance(cnt, int):
                    return cnt, "public"
                return None, "公开接口没返回作品数"
            return None, "公开接口 status=%s" % d.get("status_code")
        except Exception as e:
            err = "公开接口失败：%s" % e
    else:
        err = "没有 sec_uid"

    # 降级：登录态接口（能查已注销/受限账号，但会消耗账号风控额度）
    d = _PROFILE_HELPER.call({"uid": uid, "sec_uid": sec_uid})
    if d and d.get("ok") and isinstance(d.get("aweme_count"), int):
        return d["aweme_count"], "login"
    return None, (d or {}).get("error") or err


def count_worker(limit=COUNT_CHUNK_MAX):
    """后台批量刷新作品数：慢速 + 抖动 + 连续失败熔断，尽量不触发风控。"""
    global _COUNT_CACHE
    ok = failed = 0
    error = ""
    try:
        load_data()
        load_user_data()
        items = []
        for uid, a in (CACHE.get("accounts") or {}).items():
            sec = str(a.get("sec_uid") or "").strip()
            if sec:
                items.append((uid, sec))
        items.sort(key=lambda x: (x[0] in _COUNT_CACHE, x[1]))   # 没刷过的先刷
        items = items[:max(1, min(int(limit or COUNT_CHUNK_MAX), COUNT_CHUNK_MAX))]
        with _COUNT_LOCK:
            COUNT_STATE.update(running=True, done=0, total=len(items), succeeded=0, failed=0,
                               stop=False, error="", finished="",
                               started=time.strftime("%Y-%m-%d %H:%M:%S"))
        consec = 0
        for i, (uid, sec) in enumerate(items, 1):
            with _COUNT_LOCK:
                if COUNT_STATE.get("stop"):
                    error = "已手动停止"
                    break
            cnt, src = fetch_aweme_count(uid, sec)
            if cnt is None:
                failed += 1
                consec += 1
            else:
                ok += 1
                consec = 0
            # 写 _COUNT_CACHE 必须在锁内：_count_cache_save 会遍历它做 json.dumps，
            # 不持锁插入新 key 会撞上「dictionary changed size during iteration」
            with _COUNT_LOCK:
                if cnt is not None:
                    _COUNT_CACHE[uid] = {"count": cnt,
                                         "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                         "src": src}
                COUNT_STATE.update(done=i, succeeded=ok, failed=failed)
            if i % 20 == 0:
                _count_cache_save()
            if consec >= 20:
                error = ("连续 %d 个查不到，可能被限流了，先停在这里；过一会儿再点一次会接着刷"
                         % consec)
                break
            time.sleep(COUNT_INTERVAL * (0.75 + random.random() * 0.5))
        _count_cache_save()
    except Exception as e:
        error = "%s: %s" % (type(e).__name__, e)
    finally:
        with _COUNT_LOCK:
            COUNT_STATE.update(running=False, error=error,
                               finished=time.strftime("%Y-%m-%d %H:%M:%S"))


def _latest_at_ts(uid):
    """缓存里「上次查询时间」→ 时间戳（没查过 / 格式坏了都算 0，排序时排最前）。"""
    at = str((_LATEST_CACHE.get(uid) or {}).get("at") or "")
    try:
        return time.mktime(time.strptime(at, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return 0


def latest_worker(limit=LATEST_CHUNK_MAX, force=False):
    """后台批量联网查「最新作品发布时间」：最久没查的先查 + 慢节奏 + 连续失败熔断。

    只挑**有 SEC_UID** 的号；TTL 内查过的不重复打接口（抖音风控不认「空闲额度」，
    查同一个号第二次和第 500 次没区别，纯属浪费额度）。force=True 时忽略 TTL 全量重查。
    """
    ok = failed = skipped = empty_n = dead_n = 0
    error = ""
    try:
        load_data()
        load_user_data()
        now = time.time()
        items = []
        for uid, a in (CACHE.get("accounts") or {}).items():
            sec = str(a.get("sec_uid") or "").strip()
            if not sec:
                continue
            ts = _latest_at_ts(uid)
            if not force and ts and (now - ts) < LATEST_TTL_DAYS * 86400:
                skipped += 1
                continue
            items.append((ts, uid, sec, str(a.get("nickname") or uid)))
        # ts=0（从没查过）天然排最前，其次是最久没查的
        items.sort(key=lambda x: (x[0], x[1]))
        items = items[:max(1, min(int(limit or LATEST_CHUNK_MAX), LATEST_CHUNK_MAX))]
        with _LATEST_STATE_LOCK:
            LATEST_STATE.update(running=True, done=0, total=len(items), succeeded=0,
                                failed=0, empty=0, dead=0, skipped=skipped, stop=False,
                                error="", last_error="", pausing=False,
                                finished="", started=time.strftime("%Y-%m-%d %H:%M:%S"))
        if not items:
            error = ("%d 个号都在 %d 天内查过了，这次没有要查的（想全部重查就选「强制重查」）"
                     % (skipped, LATEST_TTL_DAYS))
            return
        consec = 0
        for i, (_ts, uid, sec, nick) in enumerate(items, 1):
            with _LATEST_STATE_LOCK:
                if LATEST_STATE.get("stop"):
                    error = "已手动停止"
                    break
            latest, err, kind = fetch_latest_online(uid, sec)
            if latest:
                ok += 1
                consec = 0
            elif kind in ("empty", "dead"):
                # 「没有公开作品」和「SEC_UID 已失效」都属于**没得查**：记下来别再烦它
                # （写空 latest 会覆盖历史值，所以只更新 at），不算失败、也不进熔断。
                if kind == "dead":
                    dead_n += 1
                else:
                    empty_n += 1
                consec = 0
                with _LATEST_LOCK:
                    prev = dict(_LATEST_CACHE.get(uid) or {})
                    prev["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    prev.setdefault("latest", "")
                    prev[kind] = True
                    _LATEST_CACHE[uid] = prev
                    _latest_cache_save()
            else:
                failed += 1
                consec += 1
            with _LATEST_STATE_LOCK:
                # 失败原因留一条给人看：不然跑完只知道「失败了 5 个」，不知道为什么
                LATEST_STATE.update(done=i, succeeded=ok, failed=failed,
                                    empty=empty_n, dead=dead_n)
                if not latest and not kind:
                    LATEST_STATE["last_error"] = "%s：%s" % (nick, err or "查不到")
            if consec >= LATEST_FAIL_BREAK:
                error = ("连续 %d 个查不到（可能是 Cookie 失效或触发风控），先停在这里；"
                         "过一会儿再点一次会接着查" % consec)
                break
            time.sleep(LATEST_INTERVAL * (0.88 + random.random() * 0.24))
            # 每 25 个歇 30 秒：风控盯的是「短时间内的请求密度」，匀速猛冲比「中间喘口气」
            # 更容易撞。下载器自己用同一个 cookie 也是每 10 个号停 60 秒。
            if (LATEST_PAUSE_EVERY and i % LATEST_PAUSE_EVERY == 0
                    and i < len(items)):
                with _LATEST_STATE_LOCK:
                    LATEST_STATE["pausing"] = True
                try:
                    for _ in range(LATEST_PAUSE_SECS):
                        with _LATEST_STATE_LOCK:
                            if LATEST_STATE.get("stop"):
                                break
                        time.sleep(1)
                finally:
                    with _LATEST_STATE_LOCK:
                        LATEST_STATE["pausing"] = False
    except Exception as e:
        error = "%s: %s" % (type(e).__name__, e)
    finally:
        with _LATEST_STATE_LOCK:
            LATEST_STATE.update(running=False, pausing=False, error=error,
                                finished=time.strftime("%Y-%m-%d %H:%M:%S"))


def dir_stats(path):
    """{files, bytes}：目录体积统计（只算文件，不算子目录）。"""
    files = 0
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_file():
                        files += 1
                        total += e.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return {"files": files, "bytes": total}


def trim_cache(path, max_mb, keep=()):
    """按修改时间从旧到新淘汰，直到目录体积 <= max_mb。keep 里的文件留一命。

    返回 {"freed", "removed", "failed"}。单个文件删不掉（被占用/权限/外部守卫）只记一次
    失败、继续删下一个——不能因为一个文件删不了就让整次清理失败。
    """
    result = {"freed": 0, "removed": 0, "failed": 0}
    limit = max_mb * 1024 * 1024
    with _CACHE_TRIM_LOCK:
        entries = []
        total = 0
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if not e.is_file():
                            continue
                        st = e.stat()
                    except OSError:
                        continue
                    total += st.st_size
                    entries.append((st.st_mtime, e.path, st.st_size))
        except OSError:
            return result
        if total <= limit:
            return result
        entries.sort()   # 最久没动过的排前面
        for _mt, p, size in entries:
            if total <= limit:
                break
            if os.path.basename(p) in keep or p.endswith(".part.mp4"):
                continue
            try:
                os.remove(p)
            except BaseException:
                # 用 BaseException 而不是 Exception：文件被占用、权限不足、以及外部
                # 环境注入的删除守卫（可能抛 SystemExit 之类的非 Exception）都要兜住，
                # 否则一个文件删不掉就会把整个 HTTP 请求打成空响应。
                result["failed"] += 1
                continue
            total -= size
            result["freed"] += size
            result["removed"] += 1
        return result


def _trim_thumbs_bg():
    """后台淘汰缩略图。

    keep（封面文件名）必须在这里算：cover_cache_names() 要读 674KB 的 user_data.json，
    以前它是在请求线程里同步执行的，每 200 张缩略图就有一次请求被解析阻塞。
    """
    try:
        trim_cache(THUMB_CACHE, THUMB_CACHE_MAX_MB, keep=cover_cache_names())
    except BaseException:
        pass          # 淘汰失败不影响出图


def wipe_cache(path, keep=()):
    """整个清空（.part.mp4 是正在写的临时文件，跳过）。返回 {"freed","removed","failed"}。

    keep 里的文件名不删：给缩略图传「封面用到的那些」——封面在 COVER_CACHE 里有独立副本，
    这里留下只是省一次截帧，但用户对「封面永远不会消失」有明确预期，全清时也照办。
    """
    result = {"freed": 0, "removed": 0, "failed": 0}
    with _CACHE_TRIM_LOCK:
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if not e.is_file() or e.path.endswith(".part.mp4"):
                            continue
                        if keep and os.path.basename(e.path) in keep:
                            continue
                        size = e.stat().st_size
                        os.remove(e.path)
                    except BaseException:
                        result["failed"] += 1
                        continue
                    result["freed"] += size
                    result["removed"] += 1
        except OSError:
            pass
    return result


def probe_media(full_path):
    """一次 ffprobe 拿到 编码 / 高度 / 时长 / 码率，带内存缓存。

    转码要用高度决定目标分辨率（原来写死 720p，1080p 的片子白白降质），
    要用时长算转码百分比，一次调用全拿回来，避免每个文件 ffprobe 两遍。
    """
    try:
        st = os.path.getmtime(full_path)
    except OSError:
        return {"codec": "", "height": 0, "duration": 0.0}
    key = (full_path, st)
    if key in PROBE_CACHE:
        return PROBE_CACHE[key]
    info = {"codec": "", "height": 0, "duration": 0.0, "bitrate": 0}
    try:
        out = subprocess.run(
            [_eff_ffprobe(), "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name,height:format=duration,bit_rate",
             "-of", "default=nw=1", full_path],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in (out.stdout or "").splitlines():
            k, _, v = line.partition("=")
            k, v = k.strip().lower(), v.strip()
            if k == "codec_name":
                info["codec"] = v.lower()
            elif k == "height":
                try:
                    info["height"] = int(float(v))
                except ValueError:
                    pass
            elif k == "duration":
                try:
                    info["duration"] = float(v)
                except ValueError:
                    pass
            elif k == "bit_rate":
                try:
                    info["bitrate"] = int(float(v))
                except ValueError:
                    pass
    except Exception:
        pass
    if len(PROBE_CACHE) > 4000:
        PROBE_CACHE.clear()
    PROBE_CACHE[key] = info
    return info


def probe_codec(full_path):
    """用 ffprobe 读视频编码，带内存缓存。失败返回 ""。"""
    return probe_media(full_path)["codec"]


def transcode_target_height(src_height):
    """目标高度：不放大、不超过 TRANS_MAX_HEIGHT。探测不到源码就用 720 兜底。"""
    if src_height and src_height > 0:
        return min(src_height, TRANS_MAX_HEIGHT)
    return 720


def transcode_bitrate(height, src_bitrate=0):
    """按源码率定目标码率。

    H.264 要同等画质大约需要 HEVC 的 1.35 倍码率，但直接按分辨率给固定高码率会让
    缓存严重膨胀（实测 1080p 源 2.8MB/996k 转成 4000k 后 12MB，4 倍）。所以：
    以源码率的 1.35 倍为准，夹在 [800k, 该分辨率的合理上限] 之间。
    """
    cap = 3500 if height >= 1080 else 2200 if height >= 720 else 1400 if height >= 540 else 900
    kb = cap
    if src_bitrate and src_bitrate > 0:
        kb = max(800, min(int(src_bitrate / 1000 * 1.35), cap))
    return "%dk" % kb


def transcode_for_browser(full_path, rel, on_progress=None):
    """把浏览器放不了的编码（HEVC 等）转成 H.264，返回可访问的相对 URL。

    on_progress(pct) 会被反复调用（0-100），长视频转码时前端才能显示真实进度。
    """
    import hashlib
    os.makedirs(TRANS_CACHE, exist_ok=True)
    name = hashlib.md5(rel.encode("utf-8")).hexdigest() + ".mp4"
    out_path = os.path.join(TRANS_CACHE, name)
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
        if on_progress:
            on_progress(100)
        return "/trans_cache/" + name

    meta = probe_media(full_path)
    src_h = meta.get("height") or 0
    target = transcode_target_height(src_h)
    duration = meta.get("duration") or 0.0
    vf = [] if (src_h and src_h <= target) else ["-vf", f"scale=-2:{target}"]
    bitrate = transcode_bitrate(target, meta.get("bitrate") or 0)

    tmp_path = out_path + ".part.mp4"
    # 编码器依次尝试：显卡（NVENC）→ 系统编码器（h264_mf）→ 软件（libopenh264）。
    # 本机 ffmpeg 是 LGPL 版，没有 libx264。
    # 实测（RTX 2060 SUPER，一段 15.7s 的 HEVC 源）：
    #   libopenh264 32.8s / h264_mf 13.5s / cuda 解码 + NVENC 8.66s，而且几乎不占 CPU。
    # NVENC 第一次失败（没 N 卡 / 驱动太老 / 分辨率超限）就把 _NVENC["ok"] 置 False，
    # 这次剩下的、以及本进程后面所有转码都直接跳过它，不再白等一次失败。
    encoders = []
    if _nvenc_ok():
        encoders.append((["-hwaccel", "cuda"],
                         ["-c:v", "h264_nvenc", "-preset", "p4", "-b:v", bitrate], "nvenc"))
    encoders.append(([], ["-c:v", "h264_mf", "-b:v", bitrate], "mf"))
    encoders.append(([], ["-c:v", "libopenh264", "-b:v", bitrate], "sw"))
    deadline = time.time() + 900
    for pre, enc, tag in encoders:
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        cmd = [_eff_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", "-nostats"] + pre + [
               "-progress", "pipe:1",     # 进度从 stdout 读，stderr 保持干净
               "-i", full_path] + vf + enc + [
               "-c:a", "aac", "-b:a", "128k",
               "-movflags", "+faststart", tmp_path]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    text=True, encoding="utf-8", errors="replace",
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            continue
        for line in (proc.stdout or []):
            if time.time() > deadline:
                proc.kill()
                break
            key, _, val = line.strip().partition("=")
            # ffmpeg 的 -progress 输出里 out_time_us / out_time_ms 都是微秒
            if key not in ("out_time_us", "out_time_ms") or not duration:
                continue
            try:
                us = int(val)
            except ValueError:
                continue
            if us < 0:
                continue
            if on_progress:
                on_progress(max(0, min(99, int(us / 1e6 / duration * 100))))
        try:
            proc.wait(timeout=max(5, deadline - time.time()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        # 判成功必须看 returncode：被 kill 掉的半截 mp4 大小也 >0，
        # 只看大小会把残缺文件当成品 os.replace 成正式缓存，而且开头
        # 「文件存在且 >0 就复用」会让它永远重建不了。
        if proc.returncode == 0 and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
            break
        if tag == "nvenc":
            # 显卡编码失败（没 N 卡 / 驱动老 / 这个分辨率不支持）：
            # 本进程后面别再试了，直接走系统编码器，省一次必然失败的重试
            _NVENC["ok"] = False
        if os.path.isfile(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    if os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
        try:
            os.replace(tmp_path, out_path)
        except OSError:
            return ""
        if on_progress:
            on_progress(100)
        return "/trans_cache/" + name
    return ""


# 媒体文件扩展名 -> MIME
MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".flv": "video/x-flv",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}

# 这些扩展名不做长缓存（改代码/改标注要立刻生效）
NO_CACHE_EXTS = {".html", ".htm", ".js", ".css", ".json", ".ico", ".svg"}

# Range 请求「区间越界」的哨兵值（None 表示按整体返回）
_RANGE_BAD = object()

# 网页文件白名单：只有这些都发，别的（包括 data.json / 源码）一律 404。
# 以前是「文件存在就发」，配合 path.lstrip("/") 可以 ../.. 读出整盘任意文件。
STATIC_FILES = {"index.html", "app.js", "style.css", "favicon.ico", "favicon.svg",
                "annotate.html", "annotate.js"}
# 前端要读的 JSON（相对路径 fetch；映射到 data/ 目录）
STATIC_DATA_MAP = {"user_data.json": "USER_DATA"}


def safe_media_path(rel):
    """把前端传来的相对路径安全地拼到 SCAN_ROOT 下。

    返回绝对路径，非法时返回 None。必须挡三类输入：
      - `..` 越级           → 能读盘上任意文件
      - 绝对路径 `C:/x`      → os.path.join 会直接丢弃前面的 SCAN_ROOT
      - 盘符/UNC            → 同上
    /thumb、/cover、/api/probe、/api/transcode、/api/open 全部走这里。
    """
    if not rel or not isinstance(rel, str):
        return None
    cleaned = rel.replace("\\", "/").strip()
    if "\x00" in cleaned:
        return None
    if cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        return None
    parts = [p for p in cleaned.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    full = os.path.join(SCAN_ROOT, *parts)
    try:
        root = os.path.realpath(SCAN_ROOT)
        real = os.path.realpath(full)
        if os.path.commonpath([root, real]) != root:
            return None
    except (OSError, ValueError):
        return None
    return full


# ===========================================================================
# 追更（watch）：盯住一批重点作者，发现新作品就下
#
# 分工要清楚：
#   · 名单和状态是**纯 JSON**，3.13 这边直接读写，随时能看；
#   · 真正联网查抖音、下文件的是 watch.py，必须跑在 3.12（curl_cffi / ABogus 是
#     3.12 编译的），所以这里只负责「起进程 / 杀进程 / 读状态」。
#   · 「跑没跑」不靠内存里的句柄（server 一重启就丢），靠 watch.py 每分钟写的
#     心跳时间戳 —— 超过 3 分钟没更新就当它停了。
# ===========================================================================

WATCH_PID_FILE = os.path.join(paths.DATA_DIR, "watch.pid")
WATCH_HEARTBEAT_LIMIT = 180     # 秒：超过这么久没心跳就算没在跑


def _watch_pid():
    try:
        with open(WATCH_PID_FILE, encoding="utf-8") as f:
            return int((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def _watch_alive(pid):
    """Windows 上判断 PID 还在不在（os.kill 在 Windows 语义不一样，别用）。"""
    if not pid:
        return False
    try:
        p = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                           capture_output=True, timeout=8)
        out = (p.stdout or b"").decode("mbcs", "ignore")
        return str(pid) in out
    except Exception:
        return False


def _watch_status():
    watch = WC.load_watch()
    state = WC.load_state()
    hb = float(state.get("heartbeat") or 0)
    pid = _watch_pid()
    running = bool(hb) and (time.time() - hb) < WATCH_HEARTBEAT_LIMIT and _watch_alive(pid)
    cfg = watch["settings"]
    if not CACHE.get("accounts"):
        # 索引还没建（比如刚启动就点进追更页）：补一次，不然「作品数」全是 0
        try:
            load_data()
        except Exception:
            pass
    accounts = CACHE.get("accounts") or {}

    rows = []
    total_dl = 0
    for a in watch.get("authors") or []:
        uid = str(a.get("uid") or "")
        s = (state.get("authors") or {}).get(uid) or {}
        acc = accounts.get(uid) or {}
        total_dl += int(s.get("downloads") or 0)
        rows.append({
            "uid": uid,
            "nickname": a.get("nickname") or acc.get("nickname") or uid,
            "sec_uid": a.get("sec_uid") or "",
            "enabled": bool(a.get("enabled", True)),
            "works": len(acc.get("works") or []),
            "in_lib": bool(acc),
            "downloads": int(s.get("downloads") or 0),
            "last_scan": s.get("last_scan") or "",
            "next_check": float(s.get("next_check") or 0),
            "is_hot": bool(s.get("is_hot")),
            # 老状态里没有 tier 字段：现算一遍，别让升级前的作者全显示成"静档"
            "tier": s.get("tier") or WC.tier_of(s, cfg)[0],
            "last_post": int(s.get("last_post") or 0),
            "last_ts": int(s.get("last_ts") or 0),
            "interval": int(a.get("interval") or 0),
            "fails": len(s.get("fails") or {}),
        })
    rows.sort(key=lambda r: (-r["downloads"], r["nickname"]))

    logs = []
    try:
        if os.path.exists(WC.LOG_FILE):
            with open(WC.LOG_FILE, encoding="utf-8", errors="replace") as f:
                logs = [ln.rstrip() for ln in f.readlines()[-60:]]
    except OSError:
        pass

    # 风控相关：今日已用额度、冷却剩余、按当前档位估算的日请求量
    day = time.strftime("%Y-%m-%d")
    req_today = int(state.get("req_today") or 0) if state.get("req_day") == day else 0
    cool_left = max(0.0, float(state.get("cool_until") or 0) - time.time())
    try:
        acc_rows = WC.pool_summary()
    except Exception:
        acc_rows = []
    n_acc = len([a for a in acc_rows if a.get("enabled")]) or 1
    try:
        est_daily = WC.estimate_daily(watch, state)
    except Exception:
        est_daily = 0
    tiers = {}
    for r in rows:
        t = r.get("tier") or ("hot" if r.get("is_hot") else "idle")
        tiers[t] = tiers.get(t, 0) + 1

    return {
        "ok": True,
        "running": running,
        "pid": pid if running else 0,
        "heartbeat": hb,
        "heartbeat_age": (time.time() - hb) if hb else None,
        "settings": cfg,
        "count": len(rows),
        "hot": tiers.get("hot", 0),
        "tiers": tiers,
        "total_downloads": total_dl,
        "req_today": req_today,
        "req_budget": int(cfg.get("daily_budget") or 0) * n_acc,
        "req_budget_one": int(cfg.get("daily_budget") or 0),
        "account_count": n_acc,
        "est_daily": est_daily,
        "cool_left": int(cool_left),
        "cool_level": int(state.get("cool_level") or 0),
        "consec_fail": int(state.get("consec_fail") or 0),
        "authors": rows,
        "logs": logs,
        "auto_start": bool(watch.get("auto_start", True)),
        "accounts": acc_rows,
        "log_file": WC.LOG_FILE,
        "python312": paths.PYTHON312,
        "python312_ok": os.path.exists(paths.PYTHON312),
    }


def _watch_start():
    st = _watch_status()
    if st.get("running"):
        return {"ok": False, "error": "追更已经在跑了"}
    if not st.get("python312_ok"):
        return {"ok": False,
                "error": "找不到 Python 3.12：%s（下载器的签名组件只能在 3.12 里跑）"
                         % paths.PYTHON312}
    if not (WC.load_watch().get("authors") or []):
        return {"ok": False, "error": "名单是空的，先加几位作者"}
    script = os.path.join(paths.SRC_DIR, "watch.py")
    if not os.path.isfile(script):
        return {"ok": False, "error": "找不到 %s" % script}
    try:
        # 输出不另存：watch.py 自己会写 data/watch.log，再存一份只会分叉
        subprocess.Popen([paths.PYTHON312, "-u", script, "--loop"],
                         cwd=paths.APP_ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        return {"ok": False, "error": "启动失败：%s: %s" % (type(e).__name__, e)}
    # 等它把心跳写出来（最多 8 秒），否则网页会立刻显示「没在跑」，像失败了一样
    for _ in range(16):
        time.sleep(0.5)
        if _watch_status().get("running"):
            break
    return {"ok": True, "status": _watch_status()}


def _watch_stop():
    pid = _watch_pid()
    if not pid or not _watch_alive(pid):
        return {"ok": False, "error": "没找到正在跑的追更进程"}
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, timeout=15,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception as e:
        return {"ok": False, "error": "停止失败：%s" % e}
    try:
        os.remove(WATCH_PID_FILE)
    except OSError:
        pass
    # 心跳清零，免得网页还显示在跑
    try:
        state = WC.load_state()
        state["heartbeat"] = 0
        WC.save_state(state)
    except Exception:
        pass
    return {"ok": True}


_WATCH_WLOCK = threading.RLock()


def _watch_locked(fn):
    """所有写 watch.json 的操作都过这把锁。

    每个入口都是「读整个文件 → 改一处 → 整个写回」。详情页点追更、
    另一个标签页开着追更面板改设置，两边撞在一起就会后写覆盖先写
    （典型症状：刚点亮的追更，几秒后那个人自己从名单里消失了）。
    锁只要进程内有效——写这个文件的只有本服务。
    """
    @functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        with _WATCH_WLOCK:
            return fn(*args, **kwargs)
    return _wrapped


@_watch_locked
def _watch_add(tokens):
    """加作者：uid / sec_uid / 主页链接 / 库里唯一的昵称。逻辑跟 watch.py --add 一致。"""
    watch = WC.load_watch()
    accounts = CACHE.get("accounts") or {}
    have = {str(a.get("uid")) for a in watch.get("authors") or []}
    by_sec = {str(a.get("sec_uid")): (uid, a) for uid, a in accounts.items() if a.get("sec_uid")}
    added, skipped, errors = [], [], []
    sec_re = re.compile(r"(MS4wLjAB[\w\-]{16,})")
    for token in tokens or []:
        t = str(token or "").strip()
        if not t:
            continue
        m = sec_re.search(t)
        sec = m.group(1) if m else ""
        uid, a = None, None
        if sec and sec in by_sec:
            uid, a = by_sec[sec]
        elif t.isdigit() and t in accounts:
            uid, a = t, accounts[t]
        elif not t.isdigit() and not sec:
            hits = [(u, x) for u, x in accounts.items()
                    if t.lower() in str(x.get("nickname") or "").lower()]
            if len(hits) == 1:
                uid, a = hits[0]
            elif len(hits) > 1:
                errors.append("「%s」匹配到 %d 位，请改用 uid" % (t, len(hits)))
                continue
        if not uid or not a:
            errors.append("认不出「%s」：库里没有它" % t)
            continue
        if str(uid) in have:
            skipped.append(a.get("nickname") or str(uid))
            continue
        watch["authors"].append({
            "uid": str(uid), "sec_uid": a.get("sec_uid") or "",
            "nickname": a.get("nickname") or "", "added_at": WC.now_str(),
            "enabled": True,
        })
        have.add(str(uid))
        added.append({"uid": str(uid), "nickname": a.get("nickname") or ""})
    if added:
        WC.save_watch(watch)
    return {"ok": True, "added": added, "skipped": skipped, "errors": errors}


@_watch_locked
def _watch_add_batch(data):
    """导出页「🤖 自动追更」：把选中的作者一次性加进追更名单。

    参数：
      uids     —— 作者 uid 列表（导出预览里选的那些）
      sec_uids —— 只有 SEC_UID 的（待下载区推来的还没建档的作者）
      interval —— 更新频率（秒）。0 / 空 = 跟着活跃度自动分级（热 15 分钟、冷 3 小时）
      start    —— 加完是否立刻开始跑（默认 True）
    """
    watch = WC.load_watch()
    accounts = CACHE.get("accounts") or {}
    by_sec = {}
    for u, a in accounts.items():
        s = str(a.get("sec_uid") or "").strip()
        if s and s not in by_sec:
            by_sec[s] = u

    interval = 0
    try:
        interval = int(float(data.get("interval") or 0))
    except (TypeError, ValueError):
        interval = 0
    if interval and interval < 60:
        interval = 60
    if interval > 7 * 86400:
        interval = 7 * 86400

    want = []            # 去重后的目标 uid，按传入顺序
    unknown = 0
    for u in (data.get("uids") or []):
        u = str(u or "").strip()
        if not u:
            continue
        if u in accounts:
            want.append(u)
        else:
            unknown += 1
    for s in (data.get("sec_uids") or []):
        s = norm_sec_uid(s)
        if not s:
            continue
        u = by_sec.get(s)
        if u:
            want.append(u)
        else:
            unknown += 1        # 库里没有 SEC_UID 对照，加进来也下不了，如实告诉用户

    seen = set()
    targets = []
    for u in want:
        if u not in seen:
            seen.add(u)
            targets.append(u)

    have = {str(a.get("uid")): a for a in (watch.get("authors") or [])}
    added = updated = 0
    for u in targets:
        a = accounts.get(u) or {}
        item = have.get(u)
        if item is not None:
            item["enabled"] = True
            if interval:
                item["interval"] = interval
            else:
                item.pop("interval", None)
            if not item.get("sec_uid"):
                item["sec_uid"] = a.get("sec_uid") or ""
            updated += 1
        else:
            nw = {
                "uid": u, "sec_uid": a.get("sec_uid") or "",
                "nickname": a.get("nickname") or "", "added_at": WC.now_str(),
                "enabled": True,
            }
            if interval:
                nw["interval"] = interval
            watch["authors"].append(nw)
            added += 1
    if added or updated:
        WC.save_watch(watch)

    started, err = False, ""
    if data.get("start", True) and (added or updated):
        st = _watch_status()
        if st.get("running"):
            started = True
        else:
            r = _watch_start()
            started = bool(r.get("ok"))
            err = "" if started else (r.get("error") or "")
    return {"ok": True, "added": added, "updated": updated,
            "unknown": unknown, "interval": interval,
            "running": started, "error": err,
            "count": len(watch.get("authors") or [])}


@_watch_locked
def _watch_remove(uids):
    watch = WC.load_watch()
    drop = {str(u) for u in (uids or [])}
    before = len(watch.get("authors") or [])
    watch["authors"] = [a for a in watch.get("authors") or [] if str(a.get("uid")) not in drop]
    WC.save_watch(watch)
    return {"ok": True, "removed": before - len(watch["authors"])}


@_watch_locked
def _watch_toggle(uid, enabled=None):
    watch = WC.load_watch()
    hit = None
    for a in watch.get("authors") or []:
        if str(a.get("uid")) == str(uid):
            a["enabled"] = (not a.get("enabled", True)) if enabled is None else bool(enabled)
            hit = a
            break
    if not hit:
        return {"ok": False, "error": "名单里没有这个 uid"}
    WC.save_watch(watch)
    return {"ok": True, "enabled": bool(hit.get("enabled"))}


def watch_of(uid):
    """这位作者在追更名单里的那一条（不在名单里返回 None）。

    watch.json 只有几百条、几 KB，每次详情页请求读一遍不心疼；
    换来的是「刷新页面后状态一定是对的」，不用在内存里再维护一份副本。
    """
    uid = str(uid or "").strip()
    if not uid:
        return None
    for a in WC.load_watch().get("authors") or []:
        if str(a.get("uid")) == uid:
            return a
    return None


@_watch_locked
def _watch_mark(uid, enabled=True, sec_uid="", nickname="", interval=None):
    """作者详情页的「自动追更」开关 / 改检查间隔：点亮就进名单，再点就退出。

    和 /api/watch 的 add 不同，这里按 uid 直接加，不靠昵称反查
    （详情页的作者一定在库里，也就不存在「同名匹配到多位」的歧义）。
    缺 SEC_UID 时明确拒绝——加进名单也下载不了，不如当场让用户先去填。

    interval：检查间隔（秒）。0 = 跟着活跃度自动分级；None = 不改动现有的值。
    """
    uid = str(uid or "").strip()
    if not uid:
        return {"ok": False, "error": "缺少 uid"}
    # 间隔先归一化：小于 1 分钟按「自动」，超过 7 天按 7 天（和 add_batch 一个口径）
    iv = None
    if interval is not None:
        try:
            iv = int(float(interval))
            iv = 0 if iv < 60 else min(iv, 7 * 86400)
        except (TypeError, ValueError):
            iv = None
    if enabled and not (CACHE.get("accounts") or {}):
        # 服务刚起来、索引还没建时也可能有人点这颗按钮：
        # 不补这一步的话查不到 SEC_UID，会误报「这位作者还没有 SEC_UID」
        try:
            load_data()
        except Exception:
            pass
    watch = WC.load_watch()
    authors = watch.get("authors") or []
    hit = next((a for a in authors if str(a.get("uid")) == uid), None)

    if enabled:
        acc = (CACHE.get("accounts") or {}).get(uid) or {}
        sec = (sec_uid or "").strip() or acc.get("sec_uid") or (hit or {}).get("sec_uid") or ""
        if not sec:
            return {"ok": False, "error": "这位作者还没有 SEC_UID，先填上才能追更"}
        nick = (nickname or "").strip() or acc.get("nickname") or (hit or {}).get("nickname") or ""
        if hit:
            # 已经在名单里但被暂停过：点一下就恢复，别当成"新加"再追加一条
            hit["enabled"] = True
            hit["sec_uid"] = sec
            if nick:
                hit["nickname"] = nick
            if iv is not None:
                hit["interval"] = iv
        else:
            authors.append({
                "uid": uid, "sec_uid": sec, "nickname": nick,
                "added_at": WC.now_str(), "enabled": True,
                "interval": iv or 0,
            })
        watch["authors"] = authors
        WC.save_watch(watch)
        return {"ok": True, "watching": True, "added": hit is None,
                "interval": iv if iv is not None else int((hit or {}).get("interval") or 0),
                "count": len(authors)}

    if not hit:
        return {"ok": True, "watching": False, "already": True, "count": len(authors)}
    watch["authors"] = [a for a in authors if str(a.get("uid")) != uid]
    WC.save_watch(watch)
    return {"ok": True, "watching": False, "count": len(watch["authors"])}


@_watch_locked
def _watch_settings(patch, auto_start=None):
    watch = WC.load_watch()
    cfg = watch["settings"]
    if auto_start is not None:
        watch["auto_start"] = bool(auto_start)
    for k, v in (patch or {}).items():
        if k not in WC.DEFAULT_CFG:
            continue
        try:
            cfg[k] = type(WC.DEFAULT_CFG[k])(v)
        except (TypeError, ValueError):
            continue
    WC.save_watch(watch)
    return {"ok": True, "settings": cfg}


def _watch_autostart():
    """服务起来后，如果名单不为空且开着 auto_start，就把追更常驻进程拉起来。

    为什么放在服务里而不是开机启动项：用户可能双击 start-all.bat、也可能只开
    start-browser.bat，路径五花八门。挂在服务上就只有一条规则 —— **服务活着，
    追更就在跑**，不用管他是怎么把服务打开的。
    """
    try:
        w = WC.load_watch()
        if not w.get("auto_start"):
            return
        n = len(w.get("authors") or [])
        if not n:
            return
        time.sleep(6)       # 等端口和索引就绪，别跟启动抢资源
        r = _watch_start()
        if r.get("ok"):
            print("[追更] 已自动开始（名单 %d 位）" % n, flush=True)
        else:
            print("[追更] 自动开始失败：%s" % r.get("error"), flush=True)
    except Exception as e:
        print("[追更] 自动开始异常：%s" % e, flush=True)


def _watch_reset_baseline(uid):
    """让某个作者下次扫描时重新建基线（换了号、或想重新对齐时间才用）。"""
    state = WC.load_state()
    st = (state.get("authors") or {}).get(str(uid))
    if st is None:
        return {"ok": False, "error": "没有这个作者的记录"}
    st["last_ts"] = 0
    st["seen"] = []
    st["fails"] = {}
    WC.save_state(state)
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 静默日志，避免刷屏
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass    # 浏览器中途关页面很常见，不值得打日志

    def _read_json_body(self):
        """读 POST 的 JSON body；空/坏 JSON 一律返回 {}（各接口自己校验字段）。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            data = json.loads(body) if body else {}
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _send_file(self, path):
        # 先 open 再 fstat：缓存淘汰线程随时会删文件，先 getsize 后 open 会有窗口期
        try:
            fp = open(path, "rb")
        except OSError:
            self._send_json({"error": "not found"}, 404)
            return
        with fp:
            try:
                size = os.fstat(fp.fileno()).st_size
            except OSError:
                self._send_json({"error": "not found"}, 404)
                return
            ext = os.path.splitext(path)[1].lower()
            mime = MIME.get(ext, "application/octet-stream")
            # 网页/JSON 必须每次回源，否则改了 app.js 浏览器还在用缓存里的旧版本
            cache = "no-cache" if ext in NO_CACHE_EXTS else "public, max-age=31536000, immutable"
            try:
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", cache)
                self.end_headers()
                # 分块写，不把整个文件读进内存（原来 fp.read()：静态白名单里现在都是小文件，
                # 但一旦有人往里加个大文件，就会整份吃掉内存 —— 2026-11-06）
                remaining = size
                while remaining > 0:
                    chunk = fp.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    @staticmethod
    def _parse_range(header, size):
        """解析 Range 头。

        返回 (start, end)；None 表示不带 Range 或格式不认（按整体返回）；
        返回 _RANGE_BAD 表示区间越界（要回 416）。
        旧实现把 `bytes=-500`（最后 500 字节）当成 0-500、start>end 时算出负长度，
        部分播放器/下载器会因此卡死或报 Content-Length mismatch。
        """
        m = re.match(r"^bytes=(\d*)-(\d*)$", (header or "").strip())
        if not m or not (m.group(1) or m.group(2)):
            return None
        a, b = m.group(1), m.group(2)
        if not a:                     # 后缀范围：最后 n 字节
            n = int(b)
            if n <= 0:
                return _RANGE_BAD
            return (max(0, size - n), size - 1)
        start = int(a)
        if start >= size:
            return _RANGE_BAD
        end = int(b) if b else size - 1
        end = min(end, size - 1)
        if end < start:
            return None
        return (start, end)

    def _send_range_file(self, path):
        """支持 Range 请求（视频播放拖动需要）"""
        try:
            fp = open(path, "rb")
        except OSError:
            # 缓存淘汰随时会删文件，这里必须回 404，不能让异常穿透成空响应
            self._send_json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(path)[1].lower()
        mime = MIME.get(ext, "application/octet-stream")
        with fp:
            try:
                size = os.fstat(fp.fileno()).st_size
            except OSError:
                self._send_json({"error": "not found"}, 404)
                return
            rng = self._parse_range(self.headers.get("Range"), size) if size > 0 else None
            if rng is _RANGE_BAD:
                try:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if rng:
                start, end = rng
                status = 206
                length = end - start + 1
            else:
                start, end = 0, max(0, size - 1)
                status = 200
                length = size
            try:
                self.send_response(status)
                self.send_header("Content-Type", mime)
                if status == 206:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                fp.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fp.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass    # 用户拖动进度条时会主动断开旧连接

    def _client_ok(self):
        """挡掉跨站请求与 DNS rebinding。

        这个服务只监听 127.0.0.1，但「本机」不等于「可信」：任意网页都能
        用 no-cors 的 simple request 向 127.0.0.1:8723 发 POST，而 /api/save
        把 null 解释为「删除字段」——被清空标注只需要一次静默请求。
        因此：Host 必须是 localhost/127.0.0.1；带 Origin 的必须是同源。
        """
        host = (self.headers.get("Host") or "").strip().lower()
        allowed_hosts = {
            "127.0.0.1:%d" % PORT, "localhost:%d" % PORT, "[::1]:%d" % PORT,
        }
        if host and host not in allowed_hosts:
            self._send_json({"ok": False, "error": "forbidden host"}, 403)
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and origin != "null":
            allowed_origins = {"http://" + h for h in allowed_hosts}
            if origin not in allowed_origins:
                self._send_json({"ok": False, "error": "forbidden origin"}, 403)
                return False
        elif origin == "null":
            # file:// 或沙箱 iframe 发来的请求，一律不接
            self._send_json({"ok": False, "error": "forbidden origin"}, 403)
            return False
        return True

    def do_GET(self):
        if not self._client_ok():
            return
        # 统一兜底（2026-11-06）：GET 侧原来一个 try 都没有，接口里任何一处
        # KeyError / TypeError / OSError 都会穿透到 socketserver —— 连接被直接关掉、
        # 响应为空，前端 resp.json() 抛 SyntaxError，只显示「列表加载失败」，
        # 真实原因只留在服务端控制台，极难自查。POST 侧一直是每个分支各自 try，这里补上。
        try:
            self._dispatch_get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:      # noqa: BLE001
            traceback.print_exc()
            try:
                self._send_json({"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    def _dispatch_get(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        # API
        if path.startswith("/api/"):
            self._handle_api_get(path)
            return

        # 转码缓存文件：同样需要 Range，才能拖动进度条
        if path.startswith("/trans_cache/"):
            name = os.path.basename(path)
            full = os.path.join(TRANS_CACHE, name)
            if os.path.isfile(full):
                self._send_range_file(full)
            else:
                self._send_json({"error": "not found"}, 404)
            return

        # 视频缩略图：ffmpeg 生成，磁盘缓存
        if path == "/thumb":
            self._handle_thumb(parse_qs(parsed.query))
            return

        # 封面图（独立目录，永不淘汰）
        if path == "/cover":
            self._handle_cover(parse_qs(parsed.query))
            return

        # 网页文件（只发白名单里的三个，src/web 下）
        if path == "/" or path == "/index.html":
            self._send_file(os.path.join(WEB_DIR, "index.html"))
            return

        # 追更面板：单独一页，不挤进主界面（主界面已经够满了）
        if path == "/watch" or path == "/watch.html":
            self._send_file(os.path.join(WEB_DIR, "watch.html"))
            return

        static_name = path.lstrip("/")
        if static_name in STATIC_FILES:
            self._send_file(os.path.join(WEB_DIR, static_name))
            return
        # 前端直接 fetch 的标注文件（物理位置在 data/，URL 保持 /user_data.json）
        if static_name in STATIC_DATA_MAP:
            self._send_file(globals()[STATIC_DATA_MAP[static_name]])
            return

        # 媒体文件：映射到 SCAN_ROOT（safe_media_path 挡 .. / 绝对路径）
        media_path = safe_media_path(static_name)
        if media_path and os.path.isfile(media_path):
            self._send_range_file(media_path)
            return

        self._send_json({"error": "not found", "path": path}, 404)

    def _handle_thumb(self, q):
        """用 ffmpeg 生成视频首帧缩略图，缓存到 thumb_cache 目录"""
        import hashlib
        rel = (q.get("path") or [""])[0]
        full = safe_media_path(rel)
        if not full or not os.path.isfile(full):
            self._send_json({"error": "not found"}, 404)
            return
        os.makedirs(THUMB_CACHE, exist_ok=True)
        cache_name = hashlib.md5(rel.encode("utf-8")).hexdigest() + ".jpg"
        cache_path = os.path.join(THUMB_CACHE, cache_name)
        if not os.path.isfile(cache_path):
            # 先写临时名再 os.replace：列表一次滚屏会并发请求几十张，两个 ffmpeg
            # 同时写同一个正式文件名时，_send_file 可能读到写了一半的 jpg，
            # 之后「文件存在」就再也不会重建，表现为一张永久破图。
            tmp_path = "%s.%d.part.jpg" % (cache_path, threading.get_ident())
            try:
                # 并发闸：列表一次滚屏会同时问几十张，放开跑只会互相抢核（见 _THUMB_SLOTS）
                with _THUMB_SLOTS:
                    ok = _thumb_ffmpeg(full, tmp_path)
                if ok and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
                    os.replace(tmp_path, cache_path)
            finally:
                if os.path.isfile(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
            if not os.path.isfile(cache_path):
                self._send_json({"error": "thumb failed"}, 500)
                return
            # 缩略图涨得很快（1.4 万张就 300MB），每 200 张检查一次上限，后台淘汰最老的
            with _CACHE_TRIM_LOCK:
                _THUMB_TRIM_TICK[0] += 1
                tick = _THUMB_TRIM_TICK[0]
            if tick % THUMB_TRIM_EVERY == 0:
                threading.Thread(target=_trim_thumbs_bg, daemon=True).start()
        self._send_file(cache_path)

    def _handle_cover(self, q):
        """封面图：写进 cover_cache，**不参与任何淘汰**。

        和老缩略图同名的直接拷过来（省一次 ffmpeg），所以历史封面升级后一张都不会丢。
        """
        rel = (q.get("path") or [""])[0]
        full = safe_media_path(rel)
        if not full or not os.path.isfile(full):
            self._send_json({"error": "not found"}, 404)
            return
        os.makedirs(COVER_CACHE, exist_ok=True)
        name = cover_thumb_name(rel)
        cache_path = os.path.join(COVER_CACHE, name)
        if not os.path.isfile(cache_path):
            old = os.path.join(THUMB_CACHE, name)
            if os.path.isfile(old):
                try:
                    shutil.copy2(old, cache_path)
                except OSError:
                    pass
        if not os.path.isfile(cache_path):
            # 截帧：先试第 1 秒（避开片头黑屏），失败退回第 0 帧
            for seek in ("1", "0"):
                try:
                    subprocess.run(
                        [_eff_ffmpeg(), "-y", "-ss", seek, "-i", full, "-frames:v", "1",
                         "-vf", "scale=480:-2", "-q:v", "3", cache_path],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=40, creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except Exception:
                    continue
                if os.path.isfile(cache_path):
                    break
        if not os.path.isfile(cache_path):
            self._send_json({"error": "cover failed"}, 500)
            return
        self._send_file(cache_path)

    # ---------- 新下载：只看「最近下载下来的那批文件」 ----------
    def _api_new_downloads(self, q):
        """按作者分组列出最近下载完成的文件。

        数据来自 data/download_log.db 的 file 表（扫描器顺手记的 rel + mtime），
        不额外读盘：mtime = 文件落盘时间 ≈ 下载完成时间。
        同一个图文作品会落 _1.._9.jpeg 好几个文件，这里按「作者 + 日期 + 描述」聚成一条，
        否则一屏里同一组图刷 9 遍；视频优先当封面。
        """
        import scanner as _sc

        def _int(name, default):
            try:
                return int(float((q.get(name) or [str(default)])[0]))
            except (TypeError, ValueError):
                return default

        since = _int("since", 0)
        uid_only = ((q.get("uid") or [""])[0] or "").strip()
        # 一页取多少个「文件」（不是一个作品：一个图文作品可能占 9 个文件）。
        # 2026-11-02 用户要求取消「只显示最新的 600 个作品」这个上限：
        # 页大小只是一个翻页节奏，滚到底自动接着取下一页，直到这个范围全部出来。
        page_files = max(1, min(_int("limit", 2400), 20000))
        cursor = ((q.get("cursor") or [""])[0] or "").strip()

        load_data()
        # 等级 / 更新频率 / 标签都存在 user_data.json 里，叠加进 CACHE 才拿得到
        load_user_data()
        accs = CACHE.get("accounts") or {}

        # 作者属性筛选 —— 和「导出作者信息」那一栏同一套口径（等级/标签/状态/更新频率/
        # 最后下载时间，每行自己一个 ＋/－ 组合开关）。先算出命中的作者，再让 SQL 按 uid 过滤，
        # 这样翻页和 total_files 都是「筛完之后」的准确值，不会出现筛了人还翻出别人的作品。
        ok_uids = None if uid_only else _attr_uids(q, accs, CACHE.get("list") or [])
        if ok_uids is not None and not ok_uids:
            self._send_json({"since": since, "until": int(time.time()), "total_files": 0,
                             "shown_items": 0, "cursor": None, "has_more": False,
                             "groups": [], "filtered_uids": 0})
            return

        total_files, rows, next_cursor, has_more = download_db.recent(
            since, page_files, uid_only or None, cursor or None, uids=ok_uids)

        groups, order, bykey = {}, [], {}
        for f in rows:
            name = f["name"] or ""
            info = _sc.parse_work_filename(name) or {}
            date = (info.get("date") or "").strip()
            desc = (info.get("desc") or "").strip() or os.path.splitext(name)[0]
            ext = os.path.splitext(name)[1].lower()
            is_video = ext in _sc.VIDEO_EXTS
            key = (f["uid"], date, desc)
            hit = bykey.get(key)
            if hit is not None:
                hit["files"] += 1
                hit["size"] += f["size"]
                if is_video:
                    if not hit["is_video"]:
                        # 之前收到的是封面图，这条其实是视频 → 换成视频，把图丢掉
                        hit["is_video"] = True
                        hit["rel"] = f["rel"]
                        hit["rels"] = [f["rel"]]
                    else:
                        hit["rels"].append(f["rel"])
                elif not hit["is_video"]:
                    # 只有图文作品才把每一张都收着（灯箱里能一张张翻）
                    hit["rels"].append(f["rel"])
                continue
            g = groups.get(f["uid"])
            if g is None:
                acc = accs.get(f["uid"]) or {}
                nick = (acc.get("nickname") or "").strip()
                if nick and _sc.looks_like_sec_uid(nick):
                    nick = ""      # 旧索引里昵称被写成 sec_uid 的那种，别往外显示
                if not nick:
                    # 刚下完还没被扫描到：从文件夹名 UIDxxx_昵称_发布作品 里兜一个
                    folder = f["rel"].split("/")[0] if "/" in f["rel"] else ""
                    m = re.match(r"^UID\d+_(.+?)_发布作品$", folder)
                    guess = (m.group(1) if m else folder).strip()
                    # 目录名那一段也可能是 sec_uid（下载器抓不到昵称时的兜底），不能当名字用
                    if _sc.looks_like_sec_uid(guess):
                        guess = ""
                    nick = guess or f["uid"]
                g = {"uid": f["uid"], "nickname": nick, "items": [],
                     # 作者身上的标注（播放区下方要显示）：等级 / 更新频率 / 状态 / 标签
                     "level": str(acc.get("level") or "").strip(),
                     "freq": str(acc.get("update_freq") or "").strip(),
                     "status": str(acc.get("status") or "").strip(),
                     "tags": [str(t).strip() for t in (acc.get("tags") or []) if str(t).strip()]}
                groups[f["uid"]] = g
                order.append(f["uid"])
            item = {"rel": f["rel"], "rels": [f["rel"]], "date": date, "desc": desc,
                    "is_video": is_video, "files": 1, "size": f["size"], "mtime": f["mtime"]}
            bykey[key] = item
            g["items"].append(item)

        # 组内已是 mtime 倒序（SQL 本来就倒着扫）；组之间按组里最新一条比
        out = [groups[u] for u in order]
        out.sort(key=lambda x: -(x["items"][0]["mtime"] if x["items"] else 0))

        # 图文作品的图片按文件名里的序号排（_1、_2 … _10）：SQL 是按 mtime 倒着扫的，
        # 不排的话灯箱里翻图顺序会是乱的
        def _natkey(r):
            return [(0, int(t)) if t.isdigit() else (1, t)
                    for t in re.split(r"(\d+)", os.path.basename(r))]
        for g in out:
            for it in g["items"]:
                if len(it["rels"]) > 1:
                    it["rels"].sort(key=_natkey)

        # 这一页的行全部给出去（不再按作品条数砍）：砍了的话被砍掉的文件
        # 在下一页里也不会再出现（cursor 已经越过它们的 mtime），就直接丢了
        for g in out:
            g["count"] = len(g["items"])

        self._send_json({
            "since": since, "until": int(time.time()),
            "total_files": total_files,
            "shown_items": sum(len(g["items"]) for g in out),
            "cursor": next_cursor, "has_more": bool(has_more),
            "groups": out,
        })

    def _handle_api_get(self, path):
        parsed_q = parse_qs(urlparse(self.path).query)

        # 新下载：最近下载完成的文件，按作者分组（数据来自 data/download_log.db）
        if path == "/api/new_downloads":
            self._api_new_downloads(parsed_q)
            return

        # 作者列表（分页 + 搜索 + 排序 + 标签/年份筛选）
        if path == "/api/authors":
            self._api_authors(parsed_q)
            return

        # 单个作者详情（含 works）
        if path == "/api/author":
            uid = (parsed_q.get("uid") or [""])[0]
            load_data()
            load_user_data()
            acc = CACHE["accounts"].get(uid)
            if acc:
                # 大小号一律用昵称显示（界面不再露 UID），把组内成员的昵称/抖音号/作品数一起带上。
                # 组内成员可能没被扫描到（手工填的 UID），这时回退 user_data 里的 nickname。
                alt_info = {}
                for au in (acc.get("alt_accounts") or []):
                    item = CACHE.get("list_index", {}).get(au) or {}
                    src = CACHE["accounts"].get(au) or {}
                    nick = (src.get("nickname") or item.get("nickname") or "").strip()
                    alt_info[au] = {
                        "nickname": nick,
                        "alias": (src.get("alias") or item.get("alias") or "").strip(),
                        "douyin_id": src.get("douyin_id") or item.get("douyin_id") or "",
                        "work_count": item.get("work_count") or (src.get("stats") or {}).get("work_count") or 0,
                    }
                acc = dict(acc)
                acc["alt_info"] = alt_info
                # 下载记录：最后一次下载（文件写入时间）与已记文件数，见 download_db.py
                try:
                    dl = download_db.author_info(uid)
                except Exception:
                    dl = {}
                acc["last_download"] = dl.get("last") or 0
                acc["download_count"] = dl.get("count") or 0
                _rlm = (CACHE.get("remote_meta") or {}).get(uid) or {}
                acc["remote_latest"] = _rlm.get("pub") or ""
                acc["remote_crawl"] = _rlm.get("crawl") or ""
                _lv = _LATEST_CACHE.get(uid) or {}
                acc["remote_live"] = _lv.get("latest") or ""
                acc["remote_live_at"] = _lv.get("at") or ""
                # 抖音上的当前作品数（缓存在 count_cache.json，详情页直接显示不用再请求）
                rc = _COUNT_CACHE.get(uid) or {}
                if rc:
                    acc["remote_count"] = rc.get("count")
                    acc["remote_at"] = rc.get("at") or ""
                # 追更标记：详情页那条「自动追更」横栏的全部状态。放在这里一起返回，
                # 刷新页面 / 换一条路点进作者时都不用再单独问一次，状态不会错。
                _w = watch_of(uid)
                acc["watching"] = bool(_w)
                acc["watch_enabled"] = bool(_w.get("enabled", True)) if _w else False
                acc["watch_interval"] = int((_w or {}).get("interval") or 0)
                if _w:
                    # watch_state.json 只有几 KB，读一次 0.2ms，顺手把「下次检查 / 已自动下几个」带上
                    try:
                        _st = (WC.load_state().get("authors") or {}).get(uid) or {}
                    except Exception:
                        _st = {}
                    acc["watch_next"] = float(_st.get("next_check") or 0)
                    acc["watch_last"] = _st.get("last_scan") or ""
                    acc["watch_downloads"] = int(_st.get("downloads") or 0)
                else:
                    acc["watch_next"] = 0
                    acc["watch_last"] = ""
                    acc["watch_downloads"] = 0
                self._send_json(acc)
            else:
                self._send_json({"error": "not found"}, 404)
            return

        # 按作品内容搜索：返回扁平化的作品列表（每个作品一条，含作者信息），
        # 供前端「作品」范围搜索时直接展示作品卡片、点进去定位到该作者的这个作品。
        if path == "/api/search_works":
            load_data()
            load_user_data()
            q_search = (parsed_q.get("q") or [""])[0].strip().lower()
            offset = int((parsed_q.get("offset") or ["0"])[0] or 0)
            limit = int((parsed_q.get("limit") or ["60"])[0] or 60)
            limit = max(1, min(limit, 200))
            accounts = CACHE["accounts"]
            hits = []
            if q_search:
                for uid, a in accounts.items():
                    works = a.get("works") or []
                    for wi, w in enumerate(works):
                        if q_search in (w.get("desc") or "").lower():
                            # 封面缩略图：优先作品自带的图，否则视频首帧
                            poster_rel = ""
                            poster_is_image = False
                            files = w.get("files") or []
                            img = next((f for f in files if os.path.splitext(f.get("rel") or "")[1].lower() in IMAGE_EXTS), None)
                            if img:
                                poster_rel = img.get("rel") or ""
                                poster_is_image = True
                            elif files:
                                poster_rel = files[0].get("rel") or ""
                            hits.append({
                                "uid": uid,
                                "nickname": a.get("nickname") or "",
                                "alias": (a.get("alias") or ""),
                                "work_index": wi,
                                "date": w.get("date") or "",
                                "desc": w.get("desc") or "",
                                "poster_rel": poster_rel,
                                "poster_is_image": poster_is_image,
                                "starred": bool(a.get("starred")),
                            })
            total = len(hits)
            hits = hits[offset:offset + limit]
            self._send_json({"ok": True, "total": total, "items": hits})
            return

        # 探测视频编码：浏览器能不能解（HEVC 基本不行）
        if path == "/api/probe":
            rel = (parsed_q.get("path") or [""])[0]
            full = safe_media_path(rel)
            if not full or not os.path.isfile(full):
                self._send_json({"ok": False, "error": "not found"}, 404)
                return
            codec = probe_codec(full)
            # 已经转过码 → 直接给转码后的地址，不用再判断编码
            cached = os.path.join(TRANS_CACHE, hashlib.md5(rel.encode("utf-8")).hexdigest() + ".mp4")
            if os.path.isfile(cached) and os.path.getsize(cached) > 0:
                self._send_json({"ok": True, "codec": codec, "playable": True,
                                 "url": "/trans_cache/" + os.path.basename(cached), "transcoded": True})
                return
            self._send_json({
                "ok": True,
                "codec": codec,
                "playable": (codec in BROWSER_OK_CODECS) if codec else True,
            })
            return

        # 转码任务进度（前端轮询）
        if path == "/api/transcode_status":
            job = (parsed_q.get("job") or [""])[0]
            with _TRANS_LOCK:
                st = TRANS_JOBS.get(job)
                st = dict(st) if st else None
            if not st:
                self._send_json({"ok": False, "status": "missing", "error": "任务不存在或已过期"}, 404)
                return
            st["ok"] = True
            self._send_json(st)
            return

        # 全量扫描的真实进度（前端轮询）
        if path == "/api/rescan_status":
            with _SCAN_LOCK:
                st = dict(SCAN_STATE)
            st["ok"] = True
            self._send_json(st)
            return

        # 批量获取资料：进度 / 结果（任务在后台线程跑，前端点一下就能走）
        if path == "/api/batch_status":
            with _BATCH_LOCK:
                st = dict(BATCH_STATE)
            st["ok"] = True
            self._send_json(st)
            return

        if path == "/api/batch_result":
            consume = (parsed_q.get("consume") or [""])[0] in ("1", "true", "yes")
            with _BATCH_LOCK:
                res = {"ok": True}
                res.update(BATCH_RESULT)
                if consume:
                    BATCH_STATE["unread"] = False
            if consume:
                # 注意：只清「未读」标记和磁盘文件，**故意不清 BATCH_RESULT 内存** ——
                # 前端进入批量页时会再拉一次（不带 consume）拿失败明细，
                # 清了用户就再也看不到「哪些账号没取到资料」了。
                try:
                    os.remove(BATCH_RESULT_PATH)
                except OSError:
                    pass
            self._send_json(res)
            return

        # 缓存体积（前端「🧹 缓存」面板）
        if path == "/api/cache_info":
            thumb = dir_stats(THUMB_CACHE)
            trans = dir_stats(TRANS_CACHE)
            cover = dir_stats(COVER_CACHE)
            self._send_json({
                "ok": True,
                "thumb": dict(thumb, max_mb=THUMB_CACHE_MAX_MB),
                "trans": dict(trans, max_mb=TRANS_CACHE_MAX_MB),
                # 封面是「数据」不是「缓存」：不设上限、不进清理，单独显示给用户看
                "cover": dict(cover, max_mb=0, protected=True),
                "total_bytes": thumb["bytes"] + trans["bytes"],
                "cover_bytes": cover["bytes"],
            })
            return

        # 抖音当前作品数：单账号（详情页点「更新」时用，1 个请求，风险极低）
        # 联网查「这个作者最新一条作品的发布时间」（真实接口，走 a_bogus 签名）
        if path == "/api/latest_work":
            uid = (parsed_q.get("uid") or [""])[0]
            # force=1：用户明确点了「↻ 刷新」才用，正常打开详情页不带这个参数。
            force = (parsed_q.get("force") or [""])[0] in ("1", "true", "yes")
            load_data()
            load_user_data()
            a = CACHE["accounts"].get(uid) or {}
            sec = str(a.get("sec_uid") or "").strip()
            if not sec:
                self._send_json({"ok": False, "error": "这个账号还没有 SEC_UID"}, 200)
                return
            # 先看服务端缓存（注意：缓存里的 ts 是「作品发布时间」，不能当查询时间用，
            # 判断新鲜度必须用 at = 查询时刻）
            with _LATEST_LOCK:
                _ent = dict(_LATEST_CACHE.get(uid) or {})
            if not force and _ent.get("latest") and _ent.get("at"):
                try:
                    _age = time.time() - time.mktime(
                        time.strptime(_ent["at"], "%Y-%m-%d %H:%M:%S"))
                except ValueError:
                    _age = None
                if _age is not None and 0 <= _age < LATEST_DETAIL_TTL:
                    self._send_json({"ok": True, "uid": uid, "latest": _ent["latest"],
                                     "at": _ent["at"], "cached": True}, 200)
                    return
            latest, err, kind = fetch_latest_online(uid, sec)
            if not latest:
                self._send_json({"ok": False, "empty": kind == "empty",
                                 "dead": kind == "dead",
                                 "error": err or "查不到最新发布时间"}, 200)
                return
            with _LATEST_LOCK:
                at = (_LATEST_CACHE.get(uid) or {}).get("at") or ""
            self._send_json({"ok": True, "uid": uid, "latest": latest, "at": at})
            return

        if path == "/api/work_count":
            uid = (parsed_q.get("uid") or [""])[0]
            load_data()
            load_user_data()
            a = CACHE["accounts"].get(uid) or {}
            sec = str(a.get("sec_uid") or "").strip()
            if not sec:
                self._send_json({"ok": False, "error": "这个账号还没有 SEC_UID，先填一下"}, 200)
                return
            cnt, src = fetch_aweme_count(uid, sec)
            if cnt is None:
                self._send_json({"ok": False, "error": src}, 200)
                return
            with _COUNT_LOCK:
                _COUNT_CACHE[uid] = {"count": cnt, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "src": src}
            _count_cache_save()
            _rlm = (CACHE.get("remote_meta") or {}).get(uid) or {}
            _lv = _LATEST_CACHE.get(uid) or {}
            self._send_json({"ok": True, "uid": uid, "count": cnt, "source": src,
                             "at": _COUNT_CACHE[uid]["at"],
                             "remote_latest": _rlm.get("pub") or "",
                             "remote_crawl": _rlm.get("crawl") or "",
                             "remote_live": _lv.get("latest") or "",
                             "remote_live_at": _lv.get("at") or ""})
            return

        # 批量刷新作品数：状态
        if path == "/api/count_status":
            with _COUNT_LOCK:
                st = dict(COUNT_STATE)
            st["ok"] = True
            st["cached"] = len(_COUNT_CACHE)
            self._send_json(st)
            return

        # 批量查「最新作品发布时间」：状态
        if path == "/api/latest_status":
            with _LATEST_STATE_LOCK:
                st = dict(LATEST_STATE)
            st["ok"] = True
            st["cached"] = len(_LATEST_CACHE)
            st["ttl_days"] = LATEST_TTL_DAYS
            self._send_json(st)
            return

        # 追更：状态（名单 + 每个作者进度 + 最近日志）
        if path == "/api/watch/status":
            self._send_json(_watch_status())
            return

        # 数据体检：账号数 / 封面数 / 最近保存 / 自动备份（用户最怕「数据被弄丢」）
        if path == "/api/data_health":
            acc = _read_user_accounts()
            covers = sum(1 for a in acc.values() if isinstance(a, dict) and a.get("cover"))
            starred = sum(1 for a in acc.values() if isinstance(a, dict) and a.get("starred"))
            statuses = {}
            for a in acc.values():
                if isinstance(a, dict) and a.get("status"):
                    statuses[a["status"]] = statuses.get(a["status"], 0) + 1
            autos = sorted(glob.glob(os.path.join(BACKUP_DIR, "user_data.auto-*.json")),
                           key=os.path.getmtime)
            try:
                mtime = os.path.getmtime(USER_DATA)
                saved_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
            except OSError:
                saved_at = ""
            self._send_json({
                "ok": True,
                "accounts": len(acc),
                "covers": covers,
                "starred": starred,
                "statuses": statuses,
                "saved_at": saved_at,
                "cover_files": dir_stats(COVER_CACHE)["files"],
                "backups": len(autos),
                "last_backup": (time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(autos[-1])))
                                if autos else ""),
            })
            return

        # 标签/年份聚合
        if path == "/api/meta":
            self._api_meta()
            return

        # 大小号分组：一「组」就是一个文件夹（左侧「大小号」入口用）
        if path == "/api/alt_groups":
            self._api_alt_groups()
            return

        # 黑名单：按行列出（含主页链接），供侧边栏「黑名单」视图
        if path == "/api/blacklist":
            self._api_blacklist()
            return

        # DouK 下载器参数（请求间隔 / 批量暂停 / 翻页上限 等）：读的是下载器自己的配置，
        # 走 src/douk_config.py 子进程（pyc 是 3.12 编的，必须 3.12 读）
        if path == "/api/douk/config":
            self._send_json(self._douk_call("read"))
            return

        # 用户设置
        if path == "/api/settings":
            s = load_settings()
            # 把路径类配置「填好」——返回实际生效的路径（用户没改过就是 paths.py 默认值），
            # 前端设置页直接显示真实地址，用户点「修改」才覆盖。
            resolved = dict(s)
            resolved["paths"] = {
                "data_dir": effective_path("data_dir", os.path.dirname(DATA_JSON)),
                "ffmpeg": effective_path("ffmpeg", FFMPEG),
                "downloader_dir": effective_path("downloader_dir", paths.DOWNLOADER_DIR),
            }
            self._send_json({
                "ok": True, "settings": resolved, "statuses": ALL_STATUS,
                "defaults": {
                    "data_dir": os.path.dirname(DATA_JSON),
                    "ffmpeg": FFMPEG,
                    "downloader_dir": paths.DOWNLOADER_DIR,
                },
            })
            return

        # 某一组重复文件里的作品明细（页面展开看缩略图用）
        if path == "/api/files/dup_group":
            name = (parsed_q.get("name") or [""])[0]
            self._send_json(_files_dup_group(name))
            return

        # 文件管理：改名候选 + 重复文件隔离区统计（一个请求拿全，页面一次渲染）
        if path == "/api/files/overview":
            self._send_json({
                "ok": True,
                "rename": _files_rename_candidates(),
                "dup": _files_dup_stats(),
                "scan": {"running": SCAN_STATE["running"], "done": SCAN_STATE["done"],
                         "total": SCAN_STATE["total"], "phase": SCAN_STATE["phase"]},
            })
            return

        # 抓收藏进度
        if path == "/api/favorites_status":
            with _FAV_LOCK:
                self._send_json(dict(FAV_STATE))
            return

        if path == "/api/open":
            self._send_json({"ok": False, "error": "请用 POST"}, 405)
            return
        self._send_json({"error": "unknown api"}, 404)

    def _api_blacklist(self):
        """黑名单按行返回（含主页链接），供侧边栏「黑名单」视图展示、编辑、删除。
        原因字段已改为「标签制」：reason 用「、」分隔多个标签，这里拆成 reasons 数组一并返回。"""
        rows = []
        tag_counter = {}
        try:
            bl = _BL.get()
            for r in bl.rows:
                sec = (r.get("sec_uid") or "").strip()
                uid = (r.get("uid") or "").strip()
                nick = (r.get("nickname") or "").strip()
                raw_reason = (r.get("reason") or "").strip()
                reasons = [t.strip() for t in re.split(r"[、,，;；]", raw_reason) if t.strip()]
                for t in reasons:
                    tag_counter[t] = tag_counter.get(t, 0) + 1
                rows.append({
                    "sec_uid": sec,
                    "uid": uid,
                    "nickname": nick,
                    "reason": raw_reason,
                    "reasons": reasons,
                    "added_at": (r.get("added_at") or "").strip(),
                    "url": ("https://www.douyin.com/user/" + sec) if sec else "",
                })
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)
            return
        # 汇总所有出现过的标签（按出现次数排序），供前端选择器复用
        all_tags = sorted(tag_counter.keys(), key=lambda t: (-tag_counter[t], t))
        self._send_json({"ok": True, "total": len(rows), "items": rows, "all_tags": all_tags})

    def _api_meta(self):
        """返回标签、年份聚合信息（前端侧边栏用）"""
        load_data()
        load_user_data()
        tag_count = {}
        year_count = {}
        starred = 0
        for uid, a in CACHE["accounts"].items():
            if not isinstance(a, dict):
                continue
            if a.get("starred"):
                starred += 1
            for t in (a.get("tags") or []):
                tag_count[t] = tag_count.get(t, 0) + 1
            for w in (a.get("works") or []):
                if isinstance(w, dict) and w.get("date"):
                    y = w["date"][:4]
                    year_count[y] = year_count.get(y, 0) + 1
        self._send_json({
            # 固定标识：_instance_alive() 用它确认「8723 上跑的是本程序」，
            # 不再依赖 total_authors 排在第几个字节（2026-11-06）
            "app": "douyin-local-library",
            "total_authors": len(CACHE["accounts"]),
            "starred": starred,
            "tags": tag_count,
            "years": year_count,
        })

    def _api_authors(self, q):
        load_data()
        load_user_data()
        lst = CACHE["list"]

        q_search = (q.get("q") or [""])[0].strip().lower()
        # all | nickname | note | signature | works | account
        # account = 只搜账号自身信息（昵称/别名/备注/抖音号/UID/sec_uid/简介），**不搜作品**。
        # 关联大小号的选择器固定用它：那边是「找一个人」，不是「找一条作品」，
        # 用 all 会把作品简介里提到这个词的作者全捞出来，搜一个词加一堆不相干的人。
        q_scope = (q.get("scope") or [""])[0].strip().lower()
        q_sort = (q.get("sort") or ["nickname"])[0]
        q_year = (q.get("year") or [""])[0]
        q_cat = (q.get("cat") or ["all"])[0]
        q_sec = (q.get("sec_uid") or [""])[0]          # has | none | ""
        q_star = (q.get("starred") or [""])[0]          # yes | no | ""
        q_cover = (q.get("cover") or [""])[0]                  # has | none | has,none（交给 _build_attr_tests）
        q_dir = (q.get("dir") or [""])[0].lower()              # asc | desc（空 = 按字段默认）
        # 属性筛选（等级/标签/状态/更新频率/最后下载时间，每行自带 ＋/－ 组合开关）
        # 抽成公共函数：「新下载」页要做成和「导出作者信息」一模一样的筛选，两边必须同口径。
        accounts = CACHE["accounts"]
        attr_test_modes = _build_attr_tests(q)

        result = []
        for item in lst:
            a = accounts.get(item["uid"])
            if q_cat == "starred" and not (a and a.get("starred")):
                continue
            # 属性筛选（只在真的选了属性时才起作用）：
            #   － 行（not）命中就剔除；＋ 行（or）至少要命中一项。
            # 全是 ＋ 是纯并集，全是 － 就是「全部作品，去掉这几样」（用户 2026-11-05 口径）。
            if attr_test_modes:
                excluded, or_any, has_or = False, False, False
                for _key, _fn, _mode in attr_test_modes:
                    hit = bool(_fn(item, a))
                    if _mode == "not":
                        if hit:
                            excluded = True
                            break
                    else:
                        has_or = True
                        if hit:
                            or_any = True
                if excluded or (has_or and not or_any):
                    continue
            # sec_uid 有/无
            sec = (item.get("sec_uid") or "").strip()
            if q_sec == "has" and not sec:
                continue
            if q_sec == "none" and sec:
                continue
            # 星标 是/否（独立于侧边栏 cat=starred）
            starred = bool(a and a.get("starred"))
            if q_star == "yes" and not starred:
                continue
            if q_star == "no" and starred:
                continue
            # 「是否有封面」已经在 _build_attr_tests 里当成一行处理了（支持 ＋/－ 圆钮），
            # 这里不再单独判一次 —— 否则用户在封面行点成 － 会和这里的「必须满足」打架。
            if q_year:
                if not (a and any(isinstance(w, dict) and (w.get("date") or "").startswith(q_year)
                                  for w in (a.get("works") or []))):
                    continue
            if q_search:
                matched = False
                if q_scope in ("", "all", "nickname", "account"):
                    if (q_search in (item.get("nickname") or "").lower()
                            or q_search in (item.get("alias") or "").lower()
                            or q_search in (item["uid"] or "").lower()
                            or q_search in (item["sec_uid"] or "").lower()
                            or q_search in (item["douyin_id"] or "").lower()):
                        matched = True
                if not matched and q_scope in ("", "all", "note", "account"):
                    if q_search in ((a or {}).get("note") or "").lower():
                        matched = True
                if not matched and q_scope in ("", "all", "signature", "account"):
                    if q_search in ((a or {}).get("signature") or "").lower():
                        matched = True
                if not matched and q_scope in ("", "all", "works"):
                    # 作品：搜每个作品的标题/简介（desc）
                    for w in (a or {}).get("works") or []:
                        if isinstance(w, dict) and q_search in (w.get("desc") or "").lower():
                            matched = True
                            break
                if not matched:
                    continue
            # 附加标签/星标状态，供列表卡片显示
            result.append({**item,
                           "alias": item.get("alias") or "",
                           "starred": bool(a and a.get("starred")),
                           "status": (a or {}).get("status", ""),
                           "level": (a or {}).get("level", ""),
                           "update_freq": (a or {}).get("update_freq", ""),
                           # 2026-11-02 c116：原来只给前 3 个标签，卡片上第三個之后的
                           # 一律看不见。用户要求「标签全部都要显示」，改成全量返回，
                           # 由前端按视图（网格卡片 / 每作者一行）自己决定怎么排。
                           "tags": list((a or {}).get("tags") or [])})

        # 排序：字段 + 方向（dir 缺省时按字段各自的习惯方向，见 SORT_DEFAULT_DIR）
        def sort_key(x):
            # 昵称一律走 .get + or ""：user_data 里若有 null（外部脚本/手工编辑造成），
            # 直接下标取值会让整个 sort 抛 "str 和 NoneType 不能比较"，
            # 进而把 /api/authors 变成空响应（2026-11-06）
            nick = x.get("nickname") or ""
            if q_sort == "uid":
                return x.get("uid") or ""
            if q_sort == "work_count":
                return x.get("work_count") or 0
            if q_sort == "video_count":
                return x.get("video_count") or 0
            if q_sort == "image_count":
                return x.get("image_count") or 0
            if q_sort == "latest":
                return (x.get("latest") or "", nick)
            if q_sort == "earliest":
                return (x.get("earliest") or "", nick)
            if q_sort == "added":
                # 添加顺序：作者文件夹创建时间（ctime），同时间再按 uid 稳定
                return (x.get("added_at") or 0, x.get("uid") or "")
            if q_sort == "download":
                # 最后下载时间：文件写入时间（= 这条作品什么时候被下下来的）
                return (x.get("last_download") or 0, x.get("uid") or "")
            if q_sort == "level":
                # 夯 > 顶级 > 一般 > 拉 > 未设置
                return (LEVEL_RANK.get(x.get("level") or "", 0), nick)
            return nick

        if q_dir in ("asc", "desc"):
            reverse = (q_dir == "desc")
        else:
            reverse = SORT_DEFAULT_DIR.get(q_sort, "asc") == "desc"
        result.sort(key=sort_key, reverse=reverse)
        if q_sort == "download" and not reverse:
            # last_download=0 = 「下载记录库里没有这个作者」（合成账号 / 没有本地作品），
            # 不是「最久没下载」，升序时压到最后，免得假装成最该重抓的那批
            result.sort(key=lambda x: not (x.get("last_download") or 0))

        total = len(result)
        try:
            offset = int((q.get("offset") or ["0"])[0])
        except ValueError:
            offset = 0
        try:
            limit = int((q.get("limit") or ["60"])[0])
        except (TypeError, ValueError):
            limit = 60
        # 边界夹紧（2026-11-06）：负 offset 会走 Python 负切片（result[-10:-10+60]），
        # limit 不设上限时 `?limit=1000000` 会先构造一个巨大的切片再序列化。
        offset = max(0, offset)
        limit = max(1, min(limit, 20000))
        page = result[offset:offset + limit]
        # 联网查到的最新发布时间（比列表构建时新，直接贴到返回项上）
        for it in page:
            c = _LATEST_CACHE.get(it["uid"]) or {}
            it["remote_live"] = c.get("latest") or ""
            it["remote_live_at"] = c.get("at") or ""

        # 「每作者一行」视图要顺带给出该作者最近 N 个作品（缩略图 + 日期 + 描述）。
        # works=N 不传 = 老行为（不给作品，纯卡片列表）。只取有本地作品的作者，
        # 按 data.json 里已有的顺序（日期倒序）取前 N 条，不再额外扫盘。
        try:
            want_works = int((q.get("works") or ["0"])[0] or 0)
        except ValueError:
            want_works = 0
        want_works = max(0, min(want_works, 40))
        if want_works:
            accounts_full = CACHE.get("accounts") or {}
            for it in page:
                src = accounts_full.get(it["uid"]) or {}
                recent = []
                for wi, w in enumerate(src.get("works") or []):
                    if len(recent) >= want_works:
                        break
                    files = w.get("files") or []
                    if not files:
                        continue
                    media = (w.get("media_type") or "").lower()
                    # 图文作品直接用它自己的第一张图（本来就是 jpg）；视频走首帧缩略图
                    if media == "image":
                        rel = files[0].get("rel") or ""
                        is_image = True
                    else:
                        rel = files[0].get("rel") or ""
                        is_image = os.path.splitext(rel)[1].lower() in IMAGE_EXTS
                    if not rel:
                        continue
                    # 图文作品把全部图都带上（右侧播放区要能一张张翻）；
                    # 视频只给首帧，翻段（_1/_2）在列表里没必要展开
                    imgs = []
                    if is_image:
                        for f in files[:9]:
                            r2 = f.get("rel") or ""
                            if r2 and os.path.splitext(r2)[1].lower() in IMAGE_EXTS:
                                imgs.append(r2)
                    recent.append({
                        "rel": rel,
                        "is_image": is_image,
                        "images": imgs,
                        "date": w.get("date") or "",
                        "desc": w.get("desc") or "",
                        "index": wi,
                        "files": len(files),
                    })
                it["recent"] = recent
                it["recent_more"] = max(0, (it.get("work_count") or 0) - len(recent))

        self._send_json({"total": total, "offset": offset, "items": page})

    def _api_alt_groups(self):
        """把大小号按「组」聚合成文件夹，供左侧「大小号」入口展示。

        一组的判定：user_data 里 alt_accounts 的连通分量（并查集）。
        组内成员可能没被扫描到（手工填的 UID）→ 这时用 user_data 里的昵称兜底。

        返回：
            {"total_groups": n, "total_accounts": m,
             "groups": [{"key", "name", "size", "members": [{uid,nickname,alias,cover,
                          douyin_id,work_count,starred,status,scanned}]}]}
        groups 只含成员 >= 2 的组；key = 成员 uid 排序后 join("|")，
        前端用 alt_group_names[key] 存自定义文件夹名。
        """
        load_data()
        load_user_data()
        acc = CACHE["accounts"]
        try:
            with open(USER_DATA, "r", encoding="utf-8") as fp:
                ud = json.load(fp)
        except (OSError, ValueError):
            ud = {}
        ann = ud.get("accounts") or {}
        names = ud.get("alt_group_names") or {}

        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for uid, ua in ann.items():
            find(uid)
            if not isinstance(ua, dict):
                continue
            arr = ua.get("alt_accounts")
            if not isinstance(arr, list):
                continue
            for v in arr:
                if isinstance(v, str) and v and v != uid:
                    union(uid, v)

        buckets = {}
        for uid in list(parent.keys()):
            buckets.setdefault(find(uid), []).append(uid)

        groups = []
        for members in buckets.values():
            if len(members) < 2:
                continue
            members = sorted(members)
            out = []
            for u in members:
                src = acc.get(u) or {}
                an = ann.get(u) or {}
                item = CACHE.get("list_index", {}).get(u) or {}
                stats = src.get("stats") or {}
                out.append({
                    "uid": u,
                    "nickname": (src.get("nickname") or an.get("nickname") or item.get("nickname") or "").strip(),
                    "alias": (an.get("alias") or "").strip(),
                    "douyin_id": src.get("douyin_id") or an.get("douyin_id") or "",
                    "cover": src.get("cover") or None,
                    "work_count": item.get("work_count") or stats.get("work_count") or 0,
                    "starred": bool(src.get("starred") or an.get("starred")),
                    "status": src.get("status") or an.get("status") or "",
                    "main_uid": an.get("main_uid") or "",
                    "scanned": u in acc,
                })
            out.sort(key=lambda x: -int(x.get("work_count") or 0))
            key = "|".join(members)
            # 默认文件夹名：组里作品最多的那个账号（有别名用别名）
            top = out[0] if out else {}
            default_name = (top.get("alias") or top.get("nickname") or top.get("uid") or "未命名组")
            groups.append({
                "key": key,
                "name": (names.get(key) or "").strip() or default_name,
                "custom": bool((names.get(key) or "").strip()),
                "size": len(members),
                "members": out,
            })

        groups.sort(key=lambda g: (-g["size"], g["name"]))
        self._send_json({
            "total_groups": len(groups),
            "total_accounts": sum(g["size"] for g in groups),
            "groups": groups,
            "names": names,
        })

    def _export_secuids(self, data):
        """把指定账号（含其所在大小号组的全部成员）的 SEC_UID 导出成 txt。

        body: {"type": "星标账号"|"注销"|..., "uids": [uid, ...]}
        每行一条 https://www.douyin.com/user/<sec_uid>；写入 export/ 目录，
        文件名 = 类型_时间戳.txt，同名自动加 -2/-3 后缀不覆盖。
        """
        load_data()
        load_user_data()
        acc = CACHE["accounts"]
        uids = [str(u) for u in (data.get("uids") or []) if str(u).strip()]
        if not uids:
            return {"ok": False, "error": "没有选中任何账号"}

        # 并查集：把请求里的账号扩展成「所在大小号组的全部成员」
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        for uid, a in acc.items():
            find(uid)
            for v in ((a or {}).get("alt_accounts") or []):
                if isinstance(v, str) and v:
                    find(v)
                    union(uid, v)
        for uid in uids:
            find(uid)

        lines, seen, missing = [], set(), 0
        sec_seen = set()   # 按 sec_uid 去重（大小号组里可能多个 uid 填了同一个 sec）
        for uid in uids:
            root = find(uid)
            for m in list(parent):
                if find(m) != root or m in seen:
                    continue
                seen.add(m)
                sec = (acc.get(m) or {}).get("sec_uid") or ""
                if sec:
                    if sec in sec_seen:
                        continue
                    sec_seen.add(sec)
                    lines.append("https://www.douyin.com/user/" + sec)
                else:
                    missing += 1
        if not lines:
            return {"ok": False, "error": "这些账号（含所在组）都没有 SEC_UID，先在详情页填写"}

        os.makedirs(EXPORT_DIR, exist_ok=True)
        safe = re.sub(r'[\\/:*?"<>|\r\n]+', "", str(data.get("type") or "导出")).strip() or "导出"
        safe = safe[:40]
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        fp = os.path.join(EXPORT_DIR, "%s_%s.txt" % (safe, stamp))
        n = 2
        while os.path.exists(fp):
            fp = os.path.join(EXPORT_DIR, "%s_%s-%d.txt" % (safe, stamp, n))
            n += 1
        with open(fp, "w", encoding="utf-8") as w:
            w.write("\n".join(lines) + "\n")
        return {"ok": True, "count": len(lines), "missing": missing,
                "file": os.path.basename(fp), "path": fp}

    def _create_author(self, data):
        """新建作者：粘主页链接 -> 查权威 uid -> 建档（user_data）-> 可选并入当前组。

        body: {"sec_uid": "https://www.douyin.com/user/MS4w...?vid=...", "link_uid": "当前账号uid"}
        资料自动获取：im/user/info 返回昵称/抖音号直接写入；之后「批量获取资料」
        也会覆盖到这个新账号（合成账号已进作者列表）。
        """
        sec = norm_sec_uid(data.get("sec_uid"))
        if not sec:
            return {"ok": False, "error": "没能认出 SEC_UID（v.douyin.com 短链不支持，请用 douyin.com/user/... 完整网址）"}
        info = douyin_query_uids([sec])
        if info.get("__error"):
            return {"ok": False, "error": "查询失败：%s（登录 cookie 可能过期，可重跑 fetch_uid_by_secuid.py 验证）" % info["__error"]}
        u = info.get(sec) or {}
        if not u.get("uid"):
            return {"ok": False, "error": "抖音没返回这个账号的 UID（账号可能已注销/封禁）"}
        uid = str(u["uid"])

        load_data()
        load_user_data()
        acc = CACHE["accounts"]
        existed = uid in acc
        link = str(data.get("link_uid") or "")
        group = set()
        if link and isinstance(acc.get(link), dict):
            group.add(link)
            for v in (acc[link].get("alt_accounts") or []):
                if isinstance(v, str) and v and v != uid:
                    group.add(v)
        group.discard(uid)

        patch = {"sec_uid": sec}
        if u.get("nickname"):
            patch["nickname"] = u["nickname"]
        if u.get("unique_id"):
            patch["douyin_id"] = u["unique_id"]

        # 全新账号：顺手把简介也抓回来（im/user/info 只给昵称/抖音号，简介要走登录态资料接口）。
        # 老账号不抓 —— 用户可能自己改过简介/备注，别拿接口的值盖掉。
        profile_note = ""
        if not existed:
            try:
                pr = fetch_profile_by_sec_uid(sec, uid)
                if pr.get("ok"):
                    for key in ("signature", "nickname", "douyin_id"):
                        val = (pr.get(key) or "").strip()
                        if val and not patch.get(key if key != "douyin_id" else "douyin_id"):
                            patch[key] = val
                    if pr.get("partial"):
                        profile_note = "简介没取到（公开接口不给简介），可在详情页点「获取资料」再试"
                else:
                    profile_note = "资料没取全：%s" % (pr.get("error") or "接口没返回")
            except Exception as e:
                profile_note = "资料没取全：%s" % e

        accounts = {uid: patch}
        for m in group:
            accounts[m] = {"alt_accounts": sorted((group | {uid}) - {m})}
        if group:
            patch["alt_accounts"] = sorted(group)

        merge_save_user_data({"accounts": accounts})
        CACHE["user_mtime"] = -1
        return {"ok": True, "uid": uid, "sec_uid": sec,
                "nickname": patch.get("nickname") or u.get("nickname") or "",
                "douyin_id": patch.get("douyin_id") or u.get("unique_id") or "",
                "signature": patch.get("signature") or "",
                "profile_note": profile_note,
                "linked": sorted(group), "existed": existed}

    def _run_fetch_favorites(self, count, source):
        """后台线程：调 3.12 子进程抓收藏/喜欢，把作者按 黑名单/已下载/未下载 分类。"""
        try:
            if not os.path.isfile(PYTHON312):
                with _FAV_LOCK:
                    FAV_STATE.update(running=False, error="找不到 Python312（下载器 curl_cffi 只支持 3.12）")
                return
            # 先加载数据，算出「已下载作者」的 sec_uid 集合 + 最新作品时间口径
            load_data()
            load_user_data()
            acc = CACHE["accounts"]
            known_sec = set()
            for uid, a in acc.items():
                if isinstance(a, dict) and a.get("sec_uid"):
                    known_sec.add(a["sec_uid"])
            latest_mode = str((load_settings().get("fav_latest_mode") or "none")).strip()
            if latest_mode not in ("none", "downloaded", "all"):
                latest_mode = "none"

            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            # 组件地址（下载器目录）若在设置里改过，传给子进程覆盖 paths.py 默认值
            _dl = effective_path("downloader_dir", "")
            if _dl:
                env["DOUK_DOWNLOADER_DIR"] = _dl
            p = subprocess.run(
                [PYTHON312, paths.HELPER_FAVORITES], env=env,
                input=json.dumps({"count": count, "source": source,
                                  "latest_mode": latest_mode,
                                  "known_sec": sorted(known_sec)}).encode("utf-8"),
                capture_output=True, timeout=600, cwd=SCRIPT_DIR)
            out = p.stdout.decode("utf-8", "replace")
            d = None
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        d = json.loads(line)
                    except ValueError:
                        continue
                    break
            if not d:
                with _FAV_LOCK:
                    FAV_STATE.update(running=False, error="抓取进程无输出：" + (p.stderr.decode('utf-8', 'replace')[:200] if p.stderr else ""))
                return
            if not d.get("ok"):
                with _FAV_LOCK:
                    FAV_STATE.update(running=False, error=d.get("error") or "抓取失败")
                return

            # 分类：黑名单 / 已下载过 / 未下载过
            load_data()
            load_user_data()
            acc = CACHE["accounts"]
            bl = _BL.get()
            # 已下载：user_data/data.json 里有这个 uid 或 sec_uid
            known_sec = set()
            sec_to_acc = {}   # sec_uid -> 本地账号 dict（uid 对不上时用 sec 反查标注）
            for uid, a in acc.items():
                if isinstance(a, dict) and a.get("sec_uid"):
                    known_sec.add(a["sec_uid"])
                    sec_to_acc.setdefault(a["sec_uid"], a)

            authors = d.get("authors") or []
            for a in authors:
                sec = a.get("sec_uid") or ""
                uid = str(a.get("uid") or "")
                if bl.is_blocked(sec_uid=sec, uid=uid, nickname=a.get("nickname") or ""):
                    a["category"] = "blacklist"
                elif (uid and uid in acc) or (sec and sec in known_sec):
                    a["category"] = "downloaded"
                else:
                    a["category"] = "new"
                a["url"] = ("https://www.douyin.com/user/" + sec) if sec else ""
                # 本地保存的最新作品日期（data.json 里 works 的最大 date；文件名只到日期）
                a["local_latest"] = ""
                a["collect_downloaded"] = False
                # 找到对应的本地账号（uid 优先，sec 兜底），附带状态/标签等标注
                local_acc = None
                if uid and uid in acc and isinstance(acc[uid], dict):
                    local_acc = acc[uid]
                elif sec and sec in sec_to_acc:
                    local_acc = sec_to_acc[sec]
                if local_acc is not None:
                    a["local_uid"] = str(local_acc.get("uid") or uid or "")
                    a["status"] = local_acc.get("status") or ""
                    a["tags"] = local_acc.get("tags") or []
                    a["update_freq"] = local_acc.get("update_freq") or ""
                    a["level"] = local_acc.get("level") or ""
                    ws = local_acc.get("works") or []
                    dates = [w.get("date") for w in ws if w.get("date")]
                    if dates:
                        a["local_latest"] = max(dates)
                    # 「最新收藏的这条作品是否已下载」：用 desc 归一化匹配本地 works
                    cd = _norm_desc(a.get("latest_desc") or "")
                    if cd:
                        for w in ws:
                            ld = _norm_desc(w.get("desc") or "")
                            if ld and (cd == ld or cd.startswith(ld) or ld.startswith(cd)):
                                a["collect_downloaded"] = True
                                break
                # 「最新作品时间」口径：查过主页且有结果 → 用真实最新发布时间覆盖
                if a.get("latest_post_time"):
                    a["last_work"] = a["latest_post_time"]

            with _FAV_LOCK:
                FAV_STATE.update(running=False, done=len(authors), total=count,
                                 result={"ok": True, "authors": authors, "total": d.get("total") or len(authors),
                                         "source": d.get("source") or source}, error="")
        except Exception as e:
            with _FAV_LOCK:
                FAV_STATE.update(running=False, error=str(e))

    def _blacklist_write(self, data):
        """黑名单增删。action=add 加一条（按 sec_uid 去重）；action=remove 按 sec_uid 删一条。"""
        action = str(data.get("action") or "add").strip()
        if action == "remove":
            sec = (data.get("sec_uid") or "").strip()
            if not sec:
                return {"ok": False, "error": "没给 sec_uid"}
            try:
                removed = _remove_blacklist(sec)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "removed": removed}
        # add
        raw_sec = (data.get("sec_uid") or "").strip()
        sec = norm_sec_uid(raw_sec)          # 兼容用户粘贴整条主页网址
        uid = str(data.get("uid") or "").strip()
        nick = (data.get("nickname") or "").strip()
        # 原因（标签制）：优先用 reasons 数组（多个标签），兼容旧的 reason 字符串
        reasons = data.get("reasons")
        if isinstance(reasons, list):
            reason = "、".join(str(t).strip() for t in reasons if str(t).strip())
        else:
            reason = (data.get("reason") or "").strip()
        if not reason:
            reason = "不需要下载"
        if not sec and not uid:
            return {"ok": False, "error": "sec_uid 和 uid 至少要有一个"}
        # 手动添加（没给昵称/uid）时，尽量查权威 uid + 昵称，让黑名单里有完整信息
        auto_lookup = data.get("lookup") is True or (not uid and not nick)
        if sec and auto_lookup:
            try:
                info = douyin_query_uids([sec])
                if info and not info.get("__error"):
                    u = info.get(sec) or {}
                    if u.get("uid"):
                        uid = str(u["uid"])
                    if u.get("nickname") and not nick:
                        nick = u["nickname"]
            except Exception:
                pass
        try:
            added = _BL.add(sec_uid=sec, uid=uid, nickname=nick, reason=reason)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "added": added, "sec_uid": sec, "uid": uid, "nickname": nick}

    def _douk_call(self, action="read", payload=None):
        """调 src/douk_config.py（必须 3.12：要 marshal 3.12 编出来的 pyc）。

        不管成功失败都返回 dict（失败带 error），别让前端拿到 500 白屏。
        """
        script = os.path.join(SCRIPT_DIR, "douk_config.py")
        if not os.path.isfile(script):
            return {"ok": False, "error": "缺少 src/douk_config.py"}
        if not os.path.isfile(PYTHON312):
            return {"ok": False, "error": "找不到 Python 3.12：%s" % PYTHON312}
        try:
            p = subprocess.run(
                [PYTHON312, script, action],
                input=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
                capture_output=True, timeout=120, cwd=SCRIPT_DIR)
            out = (p.stdout or b"").decode("utf-8", "replace").strip()
            if not out:
                err = (p.stderr or b"").decode("utf-8", "replace")[:300]
                return {"ok": False, "error": "脚本没有输出：%s" % err}
            return json.loads(out.splitlines()[-1])
        except Exception as e:
            return {"ok": False, "error": "调用失败：%s" % e}

    @staticmethod
    def _douk_mark(r):
        """DouK 建目录用的是 `UID{uid}_{mark}_发布作品`（mark 优先于昵称，见下载器
        download.py: `return user_id, mark or user_name`）。

        **2026-11-05 改**：以前这里填的是昵称 —— 昵称一改（最常见是加个 emoji）mark 就跟着变，
        下载器按新名字另建一个目录，老目录里的文件它看不见（is_skip 判的是「ID 在记录表」
        或「目标文件存在」），于是**整位作者重下一遍**，文件夹也越积越多。
        现在填「现有文件夹名里的那一段」：下载器算出来的路径和磁盘上那个目录一模一样，
        直接命中、一个都不重下。没有文件夹（新作者）才退回昵称。
        """
        f = (r.get("folder") or "").strip()
        m = re.match(r"^UID\d+_(.*)_发布作品$", f)
        if m and m.group(1).strip():
            return m.group(1)
        return r.get("nickname") or r.get("alias") or r.get("sec_uid") or ""

    def _export_authors(self, data):
        """导出作者信息，三种格式：

        format=csv   表格：昵称/别名/抖音号/UID/SEC_UID/等级/标签/状态/最新作品/最早作品/主页链接
        format=txt   一行一个主页链接（想手动粘进别处时用）
        format=json  DouK 账号数据，json_mode 决定内容：
                      settings（默认）→ 完整 settings.json，连 cookie 一起，覆盖到 Volume/ 就能跑
                      accounts        → 只要 accounts_urls 数组，方便自己合并进现有配置

        earliest_days=N：每位作者的 earliest = 「已下载作品的最新时间 - N 天」，latest 留空
                        （所以只补最近这段，不会漏边界）；不给就是全部抓取（两个字段都空）。
        """
        import csv as _csv
        from datetime import datetime, timedelta

        load_data()
        load_user_data()
        acc = CACHE["accounts"]
        index = {it.get("uid"): it for it in CACHE["list"]}

        fmt = str(data.get("format") or "csv").strip().lower()
        json_mode = str(data.get("json_mode") or "settings").strip().lower()
        # target=douk：不落 export/ 目录，直接把账号队列写进下载器自己的 settings.json（2026-10-31）
        target = str(data.get("target") or "").strip().lower()
        if target == "douk":
            fmt = "json"
        raw_days = data.get("earliest_days")
        try:
            days = int(raw_days) if raw_days not in (None, "", "null") else None
        except (TypeError, ValueError):
            days = None
        if days is not None and days < 0:
            days = None

        uids = [str(u) for u in (data.get("uids") or []) if str(u).strip()]
        # 待下载区推过来的作者常常还没有 uid（uid 只在 8790 的队列里、或者还没建档），
        # 所以除了 uids 也接受 sec_uids：能反查到期库里就带上昵称/等级/标签，
        # 查不到也照样能导出链接（DouK 的 accounts_urls 只认 url）。
        sec_only = []
        for s in (data.get("sec_uids") or []):
            s = norm_sec_uid(s)
            if s and s not in sec_only:
                sec_only.append(s)
        use_all = not uids and not sec_only
        if use_all:
            uids = list(acc.keys())

        rows = []
        for u in uids:
            a = acc.get(u)
            if not isinstance(a, dict):
                continue
            it = index.get(u) or {}
            sec = (a.get("sec_uid") or it.get("sec_uid") or "").strip()
            rows.append({
                "uid": u,
                "nickname": a.get("nickname") or it.get("nickname") or "",
                "alias": a.get("alias") or it.get("alias") or "",
                "douyin_id": a.get("douyin_id") or it.get("douyin_id") or "",
                "sec_uid": sec,
                "level": a.get("level") or "",
                "tags": a.get("tags") or [],
                "status": a.get("status") or "",
                "latest": (it.get("latest") or "")[:10],
                "earliest": (it.get("earliest") or "")[:10],
                # 本地库里有没有作品 —— 决定要不要写 DouK 的 earliest（见 _earliest_of）
                "has_works": bool(it.get("work_count") or it.get("latest")),
                "folder": it.get("folder") or a.get("folder") or "",
                "url": ("https://www.douyin.com/user/" + sec) if sec else "",
            })
        # sec_uid 来源：按 sec_uid 反查账号，查得到就把标注一并带上
        if sec_only:
            by_sec = {}
            for u, a in acc.items():
                s = (a.get("sec_uid") or "").strip()
                if s and s not in by_sec:
                    by_sec[s] = u
            have = {r["sec_uid"] for r in rows if r["sec_uid"]}
            for s in sec_only:
                if s in have:
                    continue
                have.add(s)
                u = by_sec.get(s) or ""
                a = acc.get(u) or {}
                it = index.get(u) or {}
                rows.append({
                    "uid": u,
                    "nickname": a.get("nickname") or it.get("nickname") or "",
                    "alias": a.get("alias") or it.get("alias") or "",
                    "douyin_id": a.get("douyin_id") or it.get("douyin_id") or "",
                    "sec_uid": s,
                    "level": a.get("level") or "",
                    "tags": a.get("tags") or [],
                    "status": a.get("status") or "",
                    "latest": (it.get("latest") or "")[:10],
                    "earliest": (it.get("earliest") or "")[:10],
                    "has_works": bool(it.get("work_count") or it.get("latest")),
                    "folder": it.get("folder") or a.get("folder") or "",
                    "url": "https://www.douyin.com/user/" + s,
                })

        if not rows:
            return {"ok": False, "error": "没有可导出的作者"}

        # 没有 sec_uid 的账号下不了，JSON/TXT 里直接剔掉（CSV 保留，方便用户看到缺谁）
        usable = [r for r in rows if r["sec_uid"]]

        os.makedirs(EXPORT_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

        def _pick(name, ext):
            # 扩展名必须一起参与重名判断：只比不带后缀的路径会永远「不存在」，
            # 同一秒内导出两次就会静默覆盖前一个文件。
            fp = os.path.join(EXPORT_DIR, "%s_%s%s" % (name, stamp, ext))
            i = 2
            while os.path.exists(fp):
                fp = os.path.join(EXPORT_DIR, "%s_%s-%d%s" % (name, stamp, i, ext))
                i += 1
            return fp

        # ---------- CSV ----------
        if fmt == "csv":
            fp = _pick("作者信息", ".csv")
            headers = ["昵称", "别名/备注", "抖音号", "UID", "SEC_UID", "等级", "标签", "状态",
                       "最新作品", "最早作品", "主页链接"]
            with open(fp, "w", encoding="utf-8-sig", newline="") as w:
                cw = _csv.writer(w)
                cw.writerow(headers)
                for r in rows:
                    cw.writerow([r["nickname"], r["alias"], r["douyin_id"], r["uid"], r["sec_uid"],
                                 r["level"], "、".join(r["tags"]), r["status"],
                                 r["latest"], r["earliest"], r["url"]])
            return {"ok": True, "count": len(rows), "file": os.path.basename(fp), "path": fp,
                    "format": "csv"}

        # ---------- TXT：一行一个链接 ----------
        if fmt == "txt":
            fp = _pick("DouK链接", ".txt")
            with open(fp, "w", encoding="utf-8", newline="\n") as w:
                for r in usable:
                    w.write(r["url"] + "\n")
            return {"ok": True, "count": len(usable), "skipped": len(rows) - len(usable),
                    "file": os.path.basename(fp), "path": fp, "format": "txt"}

        # ---------- JSON：DouK 账号数据 ----------
        # 用户定的规则（2026-11-01，写死在这里，改动先问）：
        #   1. 本地库里**一件作品都没有** → 不设置最早时间（earliest 留空 = DouK 全量抓）；
        #   2. 本地库**有作品** → 拿「本地最新作品日期」当基准，设置里「往前提 N 天」
        #      就往前推 N 天（填 3 = 3 天前、填 7 = 一周前）。往前推是为了不漏边界。
        # 「往前提」那一栏留空 = 不设置（导出前的提示里已经写明），此时两条规则都不写日期。
        def _earliest_of(r):
            if not days or not r["has_works"]:
                return ""
            base = r["latest"] or r["earliest"]      # 兜底：latest 万一缺了退回最早那一天
            for pat in ("%Y-%m-%d", "%Y/%m/%d"):
                try:
                    d = datetime.strptime(base, pat) - timedelta(days=days)
                except ValueError:
                    continue
                # 必须输出斜杠格式 2026/10/21：DouK 的 check_date 只认 strptime(value, '%Y/%m/%d')
                # （lib/src/interface/account.pyc）。写成横杠 2026-10-21 会报「作品最早发布日期无效」
                # 并把这个限制丢掉 → 那个作者变成全量重抓，白跑一遍。
                return d.strftime("%Y/%m/%d")
            return ""

        entries = [{
            "mark": self._douk_mark(r),
            "url": r["url"],
            "tab": "post",          # 留空 DouK 每个号都会报「tab 参数 设置错误」，填 post 干净
            "earliest": _earliest_of(r),
            "latest": "",
            "enable": True,
        } for r in usable]
        with_e = sum(1 for e in entries if e["earliest"])
        # 没有本地作品的作者：按规则 1 一律不写日期（前端提示里会说清有几个）
        no_works = sum(1 for r in usable if not r["has_works"])

        if json_mode == "accounts":
            fp = _pick("DouK账号", ".json")
            with open(fp, "w", encoding="utf-8", newline="\n") as w:
                json.dump(entries, w, ensure_ascii=False, indent=4)
            return {"ok": True, "count": len(entries), "skipped": len(rows) - len(usable),
                    "with_earliest": with_e, "no_works": no_works,
                    "file": os.path.basename(fp), "path": fp,
                    "format": "json", "json_mode": "accounts"}

        # 完整 settings.json：以现有配置为底（保留 cookie / browser_info 等），只换账号列表
        base = {}
        try:
            with open(paths.DOWNLOADER_SETTINGS, encoding="utf-8-sig") as f:
                base = json.load(f)
        except Exception:
            base = {}
        base["accounts_urls"] = entries

        # 一键写进 DouK：替换 Volume/settings.json 的账号队列（先备份旧文件）
        if target == "douk":
            return self._write_douk_settings(base, entries, with_e,
                                             len(rows) - len(usable), no_works)

        fp = _pick("DouK配置", ".json")
        with open(fp, "w", encoding="utf-8-sig", newline="\r\n") as w:
            json.dump(base, w, ensure_ascii=False, indent=4)
        return {"ok": True, "count": len(entries), "skipped": len(rows) - len(usable),
                "with_earliest": with_e, "no_works": no_works,
                "file": os.path.basename(fp), "path": fp,
                "format": "json", "json_mode": "settings", "has_cookie": bool(base.get("cookie"))}

    def _douk_running(self):
        """DouK 是不是正在跑。

        为什么要判：它在跑的时候改 settings.json 是白改 —— 退出时拿内存里的配置回写，
        用户会以为「导出了但没生效」。跟 douk_config.py 用同一套判据（tasklist）。
        """
        try:
            p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq DouK-Downloader.exe"],
                               capture_output=True, timeout=8)
            # 中文 Windows 的 tasklist 输出是 GBK，不能按 utf-8 解
            out = (p.stdout or b"").decode("mbcs", "ignore")
            return "douk-downloader.exe" in out.lower()
        except Exception:
            return False

    def _write_douk_settings(self, base, entries, with_e, skipped, no_works=0):
        """把导出的账号队列**直接写进** DouK 的 settings.json（整份替换 accounts_urls）。

        跟「导出到 export/ 目录」的区别：用户点完这里就能直接开 DouK 下载，不用自己搬文件。
        规矩：
          · 写之前把原文件备份到 backup/douk_settings_<时间>.json（同秒重名加 -2）；
          · DouK 正在跑就拒绝（见 _douk_running）；
          · 只换 accounts_urls，cookie / root / 命名格式 / 其它参数一个字不动；
          · utf-8-sig + CRLF 写临时文件再 os.replace，别让它读到半个 JSON。
        """
        if self._douk_running():
            return {"ok": False,
                    "error": "DouK 正在运行：先把它关掉再写（它退出时会用内存里的配置覆盖 settings.json）"}
        src = paths.DOWNLOADER_SETTINGS
        if not os.path.isfile(src):
            return {"ok": False, "error": "找不到 DouK 配置：%s" % src}

        os.makedirs(paths.BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(paths.BACKUP_DIR, "douk_settings_%s.json" % stamp)
        i = 2
        while os.path.exists(backup):
            backup = os.path.join(paths.BACKUP_DIR, "douk_settings_%s-%d.json" % (stamp, i))
            i += 1
        try:
            shutil.copy2(src, backup)
        except OSError as e:
            return {"ok": False, "error": "备份旧配置失败，本次没有写入：%s" % e}

        tmp = src + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8-sig", newline="\r\n") as w:
                json.dump(base, w, ensure_ascii=False, indent=4)
            os.replace(tmp, src)
        except OSError as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return {"ok": False, "error": "写 DouK 配置失败：%s" % e}

        return {"ok": True, "target": "douk", "count": len(entries), "skipped": skipped,
                "with_earliest": with_e, "no_works": no_works, "path": src,
                "backup": os.path.basename(backup), "has_cookie": bool(base.get("cookie"))}

    def do_POST(self):
        if not self._client_ok():
            return
        try:
            self._dispatch_post()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:      # noqa: BLE001
            traceback.print_exc()
            try:
                self._send_json({"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    def _dispatch_post(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/save":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body.decode("utf-8"))
                # 合并写 + 并发锁（2026-10-27 起，防清空事故 + 并发丢更新）：
                # 1) body 里出现的账号按字段合并进磁盘现有数据，没出现的账号一律保留
                #    —— 前端即使拿残缺 state 来保存，也最多新增几个账号，绝不会整库清空；
                # 2) 整个「读盘→合并→归一化→写盘」持锁执行，并发保存不会互相覆盖丢更新；
                # 3) 写盘用临时文件 + os.replace 原子替换，中途崩溃不损坏原文件。
                fixed = merge_save_user_data(data)
                if fixed:
                    print("[alt] 保存时归一化大小号：%d 条" % fixed)
                # 标注变了：下次请求重新叠加，星标/标签/状态立刻生效
                CACHE["user_mtime"] = -1
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/open":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                # _open_in_explorer 自己回响应（可能 404）
                self._open_in_explorer(data.get("path", ""))
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/transcode":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                rel = data.get("path", "")
                full = safe_media_path(rel)
                if not full or not os.path.isfile(full):
                    self._send_json({"ok": False, "error": "file not found"}, 404)
                    return
                # 已转过 → 直接给地址（秒开）
                cached = os.path.join(TRANS_CACHE, hashlib.md5(rel.encode("utf-8")).hexdigest() + ".mp4")
                if os.path.isfile(cached) and os.path.getsize(cached) > 0:
                    self._send_json({"ok": True, "url": "/trans_cache/" + os.path.basename(cached),
                                     "cached": True})
                    return
                job_id = self._start_transcode(full, rel)
                self._send_json({"ok": True, "job": job_id, "status": "running", "pct": 0})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/fetch_profile":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                sec = (data.get("sec_uid") or "").strip()
                uid = str(data.get("uid") or "").strip()
                if not sec and not uid:
                    self._send_json({"ok": False, "error": "sec_uid / uid 都为空"}, 400)
                    return
                self._send_json(fetch_profile_by_sec_uid(sec, uid))
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        # 启动 / 停止「批量获取资料」的后台任务
        if path == "/api/count_refresh":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                action = (data.get("action") or "start").strip()
                if action == "stop":
                    with _COUNT_LOCK:
                        COUNT_STATE["stop"] = True
                    self._send_json({"ok": True, "stopping": True})
                    return
                with _COUNT_LOCK:
                    if COUNT_STATE.get("running"):
                        self._send_json({"ok": False, "already": True,
                                         "error": "上一轮还在跑，先等它结束"}, 200)
                        return
                    # 抢位必须在**本线程**的锁内完成：放在 count_worker 里设 running
                    # 的话，连点两次时第二发请求看到的还是 False，会起两个 worker，
                    # 请求速率翻倍——正是风控最敏感的形态。
                    COUNT_STATE.update(running=True, stop=False, done=0, total=0,
                                       succeeded=0, failed=0, error="",
                                       started=time.strftime("%Y-%m-%d %H:%M:%S"),
                                       finished="")
                limit = int(data.get("limit") or COUNT_CHUNK_MAX)
                threading.Thread(target=count_worker, args=(limit,), daemon=True).start()
                self._send_json({"ok": True, "limit": min(limit, COUNT_CHUNK_MAX),
                                 "interval": COUNT_INTERVAL})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        # 追更：启停 / 加人 / 移除 / 改参数（一个入口，body 里带 action）
        if path == "/api/watch":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                act = (data.get("action") or "").strip()
                if act == "start":
                    self._send_json(_watch_start())
                elif act == "stop":
                    self._send_json(_watch_stop())
                elif act == "add":
                    toks = data.get("tokens")
                    if not toks:
                        one = data.get("token") or ""
                        toks = [x for x in re.split(r"[\s,，;；]+", one) if x]
                    self._send_json(_watch_add(toks))
                elif act == "add_batch":
                    # 导出页「🤖 自动追更」：选中多少位就加多少位，加完直接开跑
                    self._send_json(_watch_add_batch(data))
                elif act == "account_add":
                    self._send_json(WC.pool_add(data.get("label"), data.get("cookie")))
                elif act == "account_remove":
                    self._send_json(WC.pool_remove(data.get("id")))
                elif act == "account_toggle":
                    self._send_json(WC.pool_toggle(data.get("id"), data.get("enabled", True)))
                elif act == "account_sync":
                    self._send_json(WC.pool_sync_downloader())
                elif act == "remove":
                    self._send_json(_watch_remove(data.get("uids") or []))
                elif act == "toggle":
                    self._send_json(_watch_toggle(data.get("uid"), data.get("enabled")))
                elif act == "mark":
                    # 作者详情页的「自动追更」：enabled=true 进名单，false 退名单，
                    # interval 改检查间隔（0=自动分级，不传=不改）
                    self._send_json(_watch_mark(data.get("uid"), data.get("enabled", True),
                                                data.get("sec_uid") or "",
                                                data.get("nickname") or "",
                                                data.get("interval")))
                elif act == "settings":
                    av = data.get("auto_start")
                    self._send_json(_watch_settings(data.get("settings") or {}, av))
                elif act == "reset":
                    self._send_json(_watch_reset_baseline(data.get("uid")))
                else:
                    self._send_json({"ok": False, "error": "不认识的 action：%s" % act})
            except Exception as e:
                self._send_json({"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, e)})
            return

        # 启动 / 停止「批量联网查最新作品发布时间」的后台任务
        if path == "/api/latest_refresh":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                action = (data.get("action") or "start").strip()
                if action == "stop":
                    with _LATEST_STATE_LOCK:
                        LATEST_STATE["stop"] = True
                    self._send_json({"ok": True, "stopping": True})
                    return
                with _LATEST_STATE_LOCK:
                    if LATEST_STATE.get("running"):
                        self._send_json({"ok": False, "already": True,
                                         "error": "上一轮还在跑，先等它结束"}, 200)
                        return
                    # 同 count_refresh：抢位必须在本线程锁内，否则连点两下会起两个 worker，
                    # 请求速率翻倍 = 主动撞风控
                    LATEST_STATE.update(running=True, stop=False, done=0, total=0,
                                        succeeded=0, failed=0, skipped=0, error="",
                                        started=time.strftime("%Y-%m-%d %H:%M:%S"),
                                        finished="")
                limit = int(data.get("limit") or LATEST_CHUNK_MAX)
                force = bool(data.get("force"))
                threading.Thread(target=latest_worker, args=(limit, force),
                                 daemon=True).start()
                self._send_json({"ok": True, "limit": min(limit, LATEST_CHUNK_MAX),
                                 "force": force, "interval": LATEST_INTERVAL,
                                 "ttl_days": LATEST_TTL_DAYS})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/batch_profile":
            action = "start"
            data = {}
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                action = str(data.get("action") or "start").strip()
            except Exception:
                action = "start"
                data = {}
            if action == "stop":
                with _BATCH_LOCK:
                    if not BATCH_STATE["running"]:
                        self._send_json({"ok": False, "error": "没有正在跑的任务"}, 409)
                        return
                    BATCH_STATE["stop"] = True
                self._send_json({"ok": True, "message": "stop requested"})
                return
            with _BATCH_LOCK:
                if BATCH_STATE["running"]:
                    self._send_json({"ok": False, "already": True,
                                     "error": "上一批还在跑，等它结束"}, 409)
                    return
                BATCH_STATE.update(running=True, done=0, total=0, filled=0,
                                   conflicts=0, failed=0, skipped=0, stop=False,
                                   error="", unread=False,
                                   options=normalize_batch_options(data))
            threading.Thread(target=batch_profile_worker, args=(data,), daemon=True).start()
            self._send_json({"ok": True, "message": "batch started",
                             "options": normalize_batch_options(data)})
            return

        # 批量获取的「预演」：按当前设置算一下会跑多少个、跳过多少个（不真跑）
        if path == "/api/batch_preview":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length).decode("utf-8") if length else ""
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            opts = normalize_batch_options(data)
            targets, skipped = build_batch_targets(opts)
            counts = {}
            for a in (CACHE.get("accounts") or {}).values():
                if isinstance(a, dict):
                    st = str(a.get("status") or "").strip()
                    if st:
                        counts[st] = counts.get(st, 0) + 1
            self._send_json({"ok": True, "total": len(targets), "skipped": skipped,
                             "all": len(CACHE.get("accounts") or {}),
                             "statuses": counts, "options": opts})
            return

        if path == "/api/export_secuids":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                self._send_json(self._export_secuids(data))
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/create_author":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                self._send_json(self._create_author(data))
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/fetch_favorites":
            # 抓最近收藏/喜欢的作者。抓取要翻页联网，放后台线程跑，前端轮询 /api/favorites_status。
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            count = int(data.get("count") or 20)
            source = str(data.get("source") or "collect").strip()
            with _FAV_LOCK:
                if FAV_STATE["running"]:
                    self._send_json({"ok": False, "error": "上一次抓取还在跑，请稍候"}, 409)
                    return
                FAV_STATE.update(running=True, done=0, total=count, result=None, error="", source=source)
            threading.Thread(target=self._run_fetch_favorites, args=(count, source), daemon=True).start()
            self._send_json({"ok": True, "message": "started", "count": count, "source": source})
            return

        if path == "/api/blacklist":
            # POST 统一处理黑名单：action=add 加一条，action=remove 删一条（按 sec_uid）
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            self._send_json(self._blacklist_write(data))
            return

        if path == "/api/files/rename":
            # 把某个作者的文件夹改成指定名字（默认=你标注的昵称），并同步索引
            data = self._read_json_body()
            self._send_json(_files_rename_folder(data.get("uid"), data.get("nickname")))
            return

        if path == "/api/files/rename_batch":
            # 批量改名：body {uids:[...]} 或 {all:true}
            data = self._read_json_body()
            self._send_json(_files_rename_batch(data.get("uids"), bool(data.get("all"))))
            return

        if path == "/api/works/delete":
            # 删除作品：{uid, idxs:[作品下标...], permanent?:bool}
            # 默认**移入回收站**（文件搬到 recycle-bin/<批次>/，作品从索引摘掉），可以恢复；
            # permanent=true 才是真删（目前网页不用，留给脚本/以后）。
            body = self._read_json_body()
            self._send_json(_works_delete(body.get("uid"), body.get("idxs"),
                                          bool(body.get("permanent"))))
            return

        if path == "/api/recycle/list":
            self._send_json(_recycle_list())
            return

        if path == "/api/recycle/detail":
            # 某一批里到底有哪些作品（点开回收站那一行时查）
            body = self._read_json_body()
            self._send_json(_recycle_detail(body.get("id")))
            return

        if path == "/api/recycle/restore":
            body = self._read_json_body()
            self._send_json(_recycle_restore(body.get("id")))
            return

        if path == "/api/recycle/purge":
            body = self._read_json_body()
            self._send_json(_recycle_purge(body.get("id")))
            return

        if path == "/api/recycle/purge_all":
            # 一键清空回收站（所有批次真删，不可恢复）
            self._read_json_body()
            self._send_json(_recycle_purge_all())
            return

        if path == "/api/files/dup_resolve":
            # 按选择处理重复作品：{decisions:[{group, key:[日期,描述,图序,扩展名], keep}]}
            # keep = "current"（保留正在用的那份）或隔离区里的文件名（用它覆盖回去）
            data = self._read_json_body()
            self._send_json(_files_dup_resolve(data.get("decisions")))
            return

        if path == "/api/files/dup_delete":
            # 删隔离区里的重复文件：
            #   {names:[组目录...]} 删整组 / {all:true} 全删 / {group, files:[文件名]} 只删组内几条
            data = self._read_json_body()
            self._send_json(_files_dup_delete(data.get("names"), bool(data.get("all")),
                                              data.get("group"), data.get("files")))
            return

        if path == "/api/settings":
            data = self._read_json_body()
            try:
                s = save_settings(data)
                self._send_json({"ok": True, "settings": s})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/douk/config":
            # 保存 DouK 参数：settings.json 里的白名单字段 + pyc 里的请求间隔/批量暂停
            data = self._read_json_body()
            self._send_json(self._douk_call("write", data))
            return

        if path == "/api/export_authors":
            # 导出作者信息：body 带 uids（或全量），把 nickname/别名/抖音号/uid/sec_uid/等级/标签/状态/主页 导出成 csv
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
            except Exception:
                data = {}
            self._send_json(self._export_authors(data))
            return

        if path == "/api/cache_clean":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                data = json.loads(body) if body else {}
                target = data.get("target") or "all"
                wipe = bool(data.get("wipe"))
                pairs = []
                if target in ("thumb", "all"):
                    pairs.append((THUMB_CACHE, THUMB_CACHE_MAX_MB))
                if target in ("trans", "all"):
                    pairs.append((TRANS_CACHE, TRANS_CACHE_MAX_MB))
                freed = 0
                removed = 0
                failed = 0
                # 缩略图清理要避开「封面用到的文件」：和自动淘汰（trim 定时任务）保持同一口径。
                # 封面本体已经在 COVER_CACHE 里（永不清理），这里是第二道保险——thumb 里的
                # 那份副本也别删，省得重新截帧。
                keep_names = cover_cache_names()
                for d, cap in pairs:
                    keep = keep_names if d == THUMB_CACHE else ()
                    r = wipe_cache(d, keep=keep) if wipe else trim_cache(d, cap, keep=keep)
                    freed += r["freed"]
                    removed += r["removed"]
                    failed += r["failed"]
                thumb = dir_stats(THUMB_CACHE)
                trans = dir_stats(TRANS_CACHE)
                self._send_json({"ok": True, "freed": freed, "removed": removed,
                                 "failed": failed,
                                 "thumb": thumb, "trans": trans,
                                 "total_bytes": thumb["bytes"] + trans["bytes"]})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/rescan":
            with _SCAN_LOCK:
                if SCAN_STATE["running"]:
                    self._send_json({"ok": False, "error": "已经在扫描中，请等这次扫完"}, 409)
                    return
                SCAN_STATE.update(running=True, phase="启动扫描", done=0, total=0,
                                  started=time.strftime("%Y-%m-%d %H:%M:%S"),
                                  finished="", error="")
            threading.Thread(target=self._run_scan, daemon=True).start()
            self._send_json({"ok": True, "message": "scan started"})
            return

        if path == "/api/annotations":
            # 标注台保存界面意见：md 给人和 AI 看，json 留结构化数据
            try:
                self._save_annotations(self._read_json_body())
            except Exception as e:
                self._send_json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
            return

        self._send_json({"error": "unknown api"}, 404)

    def _save_annotations(self, data):
        """标注台保存：data/annotations/界面意见_<时间戳>.md + .json。

        文件名和目录全部由服务端拼，前端传什么都不会影响落盘位置（防路径穿越）。
        md 是用户/AI 直接读的清单，json 是结构化明细（坐标、选择器、备注）。
        """
        items = data.get("items")
        if not isinstance(items, list) or not items:
            self._send_json({"ok": False, "error": "没有标注内容"}, 400)
            return
        if len(items) > 500:
            self._send_json({"ok": False, "error": "标注太多（上限 500 条）"}, 400)
            return

        def _num(v):
            try:
                return round(float(v), 1)
            except (TypeError, ValueError):
                return 0.0

        def _str(v, n=200):
            return str(v if v is not None else "")[:n]

        clean = []
        for it in items:
            if not isinstance(it, dict):
                continue
            r = it.get("rect") if isinstance(it.get("rect"), dict) else {}
            rect = {k: _num(r.get(k)) for k in ("x", "y", "w", "h")}
            if rect["w"] < 1 and rect["h"] < 1 and it.get("tool") != "text":
                continue                      # 空框（手抖点一下）直接丢，别污染清单
            rec = {
                "tool": _str(it.get("tool"), 16),
                "region": _str(it.get("region"), 80),
                "selector": _str(it.get("selector"), 300),
                "note": _str(it.get("note"), 2000),
                "rect": rect,
                "scroller": _str(it.get("scroller"), 32),
            }
            pts = it.get("points")
            if isinstance(pts, list) and pts:
                rec["points"] = [{"x": _num(p.get("x")), "y": _num(p.get("y"))}
                                 for p in pts[:4000] if isinstance(p, dict)]
            clean.append(rec)

        if not clean:
            self._send_json({"ok": False, "error": "没有有效标注"}, 400)
            return

        text = data.get("text") if isinstance(data.get("text"), str) else ""
        if len(text) > 4 * 1024 * 1024:
            self._send_json({"ok": False, "error": "内容太大"}, 400)
            return

        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        os.makedirs(ANNOTATE_DIR, exist_ok=True)
        base = os.path.join(ANNOTATE_DIR, "界面意见_" + time.strftime("%Y%m%d-%H%M%S"))
        md = base + ".md"
        n = 2
        while os.path.exists(md):             # 同一秒点两次不覆盖
            md = "%s-%d.md" % (base, n)
            n += 1
        try:
            with open(md, "w", encoding="utf-8") as fp:
                fp.write(text or "# 界面标注清单\n")
            with open(os.path.splitext(md)[0] + ".json", "w", encoding="utf-8") as fp:
                json.dump({"saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "meta": meta, "items": clean},
                          fp, ensure_ascii=False, indent=2)
        except OSError as e:
            self._send_json({"ok": False, "error": "写文件失败：%s" % e}, 500)
            return
        self._send_json({"ok": True, "file": os.path.basename(md),
                         "dir": ANNOTATE_DIR, "count": len(clean)})

    def _open_in_explorer(self, rel):
        """在资源管理器打开文件所在目录并选中文件"""
        full = safe_media_path(rel)
        if not full or not os.path.exists(full):
            self._send_json({"ok": False, "error": "文件不存在"}, 404)
            return
        # /select, 和路径必须拼成一个参数，否则含空格的路径会选中错误位置
        subprocess.Popen(["explorer", "/select," + full.replace("/", "\\")])
        self._send_json({"ok": True})

    def _run_scan(self):
        """手动扫描（前端「重扫」按钮）：直接跑增量，进度照常报。"""
        scan_job(auto=False, changes=0)

    def _start_transcode(self, full, rel):
        """起转码线程，立刻返回 job id（不阻塞请求）。同一文件并发点开时复用同一任务。"""
        with _TRANS_LOCK:
            old = TRANS_BY_REL.get(rel)
            if old and (TRANS_JOBS.get(old) or {}).get("status") == "running":
                return old
            job_id = hashlib.md5((rel + str(time.time())).encode("utf-8")).hexdigest()[:12]
            TRANS_JOBS[job_id] = {"status": "running", "pct": 0, "url": "", "rel": rel,
                                  "error": "", "started": time.strftime("%H:%M:%S")}
            TRANS_BY_REL[rel] = job_id
        threading.Thread(target=self._transcode_worker, args=(job_id, full, rel),
                         daemon=True).start()
        return job_id

    def _transcode_worker(self, job_id, full, rel):
        def on_pct(pct):
            with _TRANS_LOCK:
                st = TRANS_JOBS.get(job_id)
                if st is not None:
                    st["pct"] = pct

        url = ""
        err = ""
        try:
            url = transcode_for_browser(full, rel, on_progress=on_pct)
        except Exception as e:
            err = str(e)
        # 转码产物也算缓存：超上限就把最久没动过的淘汰掉（刚转好的这个留着）
        if url:
            try:
                trim_cache(TRANS_CACHE, TRANS_CACHE_MAX_MB,
                           keep={os.path.basename(url)})
            except Exception:
                pass
        with _TRANS_LOCK:
            st = TRANS_JOBS.get(job_id)
            if st is not None:
                if url:
                    st.update(status="done", pct=100, url=url, error="")
                else:
                    st.update(status="error", error=err or "转码失败（本地 ffmpeg 两种编码器都没成功）")
            if TRANS_BY_REL.get(rel) == job_id:
                TRANS_BY_REL.pop(rel, None)
            # 任务表别无限增长：超过 200 条就清掉最老的一批已完成任务
            if len(TRANS_JOBS) > 200:
                done_ids = [k for k, v in TRANS_JOBS.items() if v.get("status") != "running"]
                for k in done_ids[:100]:
                    TRANS_JOBS.pop(k, None)


def _norm_desc(s):
    """作品描述归一化：去空白/标点/emoji，用于「收藏作品 vs 本地作品」的 desc 匹配。"""
    s = re.sub(r"[\s\*＊·•\-—_\.。，,、>_<＞＜（）()\[\]【】#]+", "", str(s or ""))
    s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2190-\u21FF]", "", s)
    return s.lower()


def norm_sec_uid(value):
    """把用户可能粘进来的整条主页网址抽成纯 sec_uid（与前端 parseSecUid 保持同一套规则）。

    手动填的时候人一般直接复制地址栏：https://www.douyin.com/user/MS4w...?vid=123
    如果原样存在 sec_uid 字段里，拼出来的主页地址和抖音接口参数都是坏的。
    """
    v = re.sub(r"[\s\u200b\u200c\u200d\u200e\u200f\ufeff]+", "", str(value or ""))
    if not v:
        return ""
    m = re.search(r"/user/([A-Za-z0-9_-]+)", v)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", v):
        return v
    return ""


def _remove_blacklist(sec_uid):
    """按 sec_uid 从黑名单 CSV 删一条，返回是否真删到。"""
    if not os.path.isfile(paths.BLACKLIST_FILE):
        return False
    import csv as _csv
    rows = []
    removed = False
    with open(paths.BLACKLIST_FILE, encoding="utf-8-sig", newline="") as f:
        rd = _csv.DictReader(f)
        fields = rd.fieldnames or []
        for r in rd:
            if (r.get("sec_uid") or "").strip() == sec_uid.strip():
                removed = True
                continue
            rows.append(r)
    if not removed:
        return False
    with open(paths.BLACKLIST_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    # 让 blacklist 模块的进程内缓存失效
    _BL._CACHE = None
    return True


_LATEST_LOCK = threading.Lock()
_LATEST_CACHE = {}


def _latest_cache_load():
    global _LATEST_CACHE
    try:
        with open(LATEST_CACHE_PATH, "r", encoding="utf-8") as fp:
            d = json.load(fp)
    except (OSError, ValueError):
        _LATEST_CACHE = {}
        return
    _LATEST_CACHE = d.get("authors") if isinstance(d, dict) and isinstance(d.get("authors"), dict) else {}


def _latest_cache_save():
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        tmp = LATEST_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"version": 1, "authors": _LATEST_CACHE}, fp, ensure_ascii=False)
        os.replace(tmp, LATEST_CACHE_PATH)
    except OSError as e:
        print("[latest] 写缓存失败：%s" % e, flush=True)


class LatestHelper:
    """常驻 3.12 子进程：一行一 JSON 查「作者最新作品发布时间」（签名 + cookie 在子进程里）。

    为什么要单独一个进程而不是复用它 ProfileHelper：那边跑的是 im/user/info（只给作品数），
    这边要 a_bogus 签名 + curl_cffi 浏览器指纹去打 aweme/post，模块和会话都不一样，
    混在一起任何一边出错都会把另一边带挂。
    """

    def __init__(self, script):
        self.script = script
        self.proc = None
        self.q = queue.Queue()
        self.lock = threading.Lock()

    def _kill(self):
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def _read_loop(self, proc, q):
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)

    def _ensure(self):
        if self.proc and self.proc.poll() is None:
            return True
        self._kill()
        if not os.path.isfile(PYTHON312):
            return False
        # 每个子进程配一个独立队列：旧进程被 kill 后它的读线程还可能把最后几行
        # （包括结尾的 None）塞进队列，共用一个队列时下一次请求会读到上一条的响应，
        # 表现为「这个作者的时间写成了上一个作者的」——ProfileHelper 踩过同样的坑。
        q = queue.Queue()
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            self.proc = subprocess.Popen(
                [PYTHON312, self.script], cwd=SCRIPT_DIR, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            self.proc = None
            return False
        self.q = q
        threading.Thread(target=self._read_loop, args=(self.proc, q), daemon=True).start()
        return True

    def call(self, payload, timeout=30):
        with self.lock:
            if not self._ensure():
                return None
            q = self.q
            try:
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except Exception:
                self._kill()
                return None
            try:
                line = q.get(timeout=timeout)
            except queue.Empty:
                self._kill()
                return None
            if line is None:
                self._kill()
                return None
            try:
                return json.loads(line)
            except ValueError:
                return None


_LATEST_HELPER = LatestHelper(os.path.join(SCRIPT_DIR, "latest_helper.py"))
_latest_cache_load()      # 启动先把上次联网查到的读进内存


def fetch_latest_online(uid, sec_uid):
    """联网查这个作者最新一条作品的发布时间，成功就写缓存。

    返回 (latest, error, kind)，kind 取值：
        ""      → 查到了 / 真失败（error 里有原因）
        "empty" → 对方根本没有公开作品（私密/封禁/全删），不是失败
        "dead"  → SEC_UID 已失效（账号注销或换号），要去重新补
    empty / dead 都不算失败——批量跑的时候不能进熔断计数，否则一串私密号
    会被误判成被限流。
    """
    if not sec_uid:
        return "", "这个账号没有 SEC_UID", ""
    res = _LATEST_HELPER.call({"uid": uid, "sec_uid": sec_uid}, timeout=40)
    if not res:
        return "", "查询进程没响应（Python312 / 下载器 lib 是否还在？）", ""
    if not res.get("ok"):
        kind = "dead" if res.get("dead") else ("empty" if res.get("empty") else "")
        return "", (res.get("error") or "查不到"), kind
    latest = str(res.get("latest") or "")
    with _LATEST_LOCK:
        _LATEST_CACHE[uid] = {"latest": latest, "ts": res.get("ts") or 0,
                              "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _latest_cache_save()
    return latest, "", ""


class ProfileHelper:
    """常驻的 3.12 子进程：一行一 JSON 对话，避免每个账号都重启解释器。

    curl_cffi 只有 3.12 版本，服务端跑 3.13，所以资料查询只能外包给子进程。
    串行调用用一把锁；读结果用队列 + 超时，卡死就杀掉进程下次重建。
    """

    def __init__(self):
        self.proc = None
        self.q = queue.Queue()
        self.lock = threading.Lock()

    def _kill(self):
        proc, self.proc = self.proc, None
        if proc:
            try:
                proc.kill()
                proc.wait(timeout=5)     # 不 wait 会留僵尸进程，句柄也不释放
            except Exception:
                pass
        while True:                      # 清掉残留的响应，避免错位
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def _read_loop(self, proc, q):
        try:
            for line in proc.stdout:
                q.put(line)
        except Exception:
            pass
        q.put(None)                      # 进程结束标记

    def _ensure(self):
        if self.proc and self.proc.poll() is None:
            return True
        self._kill()
        # 每个子进程配一个**独立的队列**：旧进程的 _read_loop 在 kill 之后还可能
        # 把最后几行塞进队列，用同一个队列的话，下一次请求会读到上一条响应——
        # 表现为「A 的昵称/简介写进了 B」，正是历史串号事故的形态。
        q = queue.Queue()
        try:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"   # Windows 管道默认 GBK，必须纠正
            env["PYTHONUTF8"] = "1"
            self.proc = subprocess.Popen(
                [PYTHON312, HELPER_PROFILE], cwd=SCRIPT_DIR, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            self.proc = None
            return False
        self.q = q
        threading.Thread(target=self._read_loop, args=(self.proc, q), daemon=True).start()
        return True

    def call(self, payload, timeout=25):
        with self.lock:
            if not self._ensure():
                return None
            try:
                self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            except Exception:
                self._kill()
                return None
            try:
                line = self.q.get(timeout=timeout)
            except queue.Empty:
                self._kill()
                return None
            if line is None:
                self._kill()
                return None
            try:
                return json.loads(line)
            except ValueError:
                return None


_PROFILE_HELPER = ProfileHelper()


def fetch_profile_by_sec_uid(sec_uid, uid=""):
    """获取账号资料：优先走登录态接口（能拿简介），失败再降级到公开接口。

    公开接口 iesdouyin 现在把 signature 返回成空串，所以只把它当兜底——
    兜底回来的结果会带 `partial: true`，前端据此提示「简介没取到」。
    """
    sec_uid = norm_sec_uid(sec_uid) or str(sec_uid or "").strip()
    uid = str(uid or "").strip()
    if not sec_uid and not uid:
        return {"ok": False, "error": "没能从输入里认出 SEC_UID（v.douyin.com 短链不支持，请用 douyin.com/user/... 的主页地址）"}

    d = _PROFILE_HELPER.call({"uid": uid, "sec_uid": sec_uid})
    # 乱码护栏：子进程编码不对时解出来会带 U+FFFD（�）。这种值一旦写进
    # user_data.json 就成了脏数据，宁可报错让用户重试，也不要落盘。
    if d and d.get("ok"):
        for k in ("nickname", "signature", "douyin_id"):
            if isinstance(d.get(k), str) and "\ufffd" in d[k]:
                return {"ok": False, "error": "接口返回的内容编码异常（乱码），已丢弃，请重试"}
    if d and d.get("ok") and (d.get("nickname") or d.get("signature")):
        d["source"] = "login"
        return d

    if sec_uid:
        r = fetch_profile_public(sec_uid)
        if r.get("ok"):
            r["partial"] = True          # 公开接口拿不到简介
            r["source"] = "public"
            if d and not d.get("ok") and d.get("error"):
                r["helper_error"] = d["error"]
            return r
    if d and not d.get("ok"):
        return d
    return {"ok": False, "error": "获取失败（接口没返回数据）"}


def _batch_result_save(result):
    """批量结果落盘（前端取走即删）。服务重启后还能把上次结果交给前端。"""
    tmp = BATCH_RESULT_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump(result, fp, ensure_ascii=False)
        os.replace(tmp, BATCH_RESULT_PATH)
    except OSError:
        pass


def _batch_result_load():
    """启动时把上次没被取走的结果读回来，前端打开页面会收到。"""
    try:
        with open(BATCH_RESULT_PATH, "r", encoding="utf-8") as fp:
            d = json.load(fp)
    except (OSError, ValueError):
        return
    if isinstance(d, dict):
        with _BATCH_LOCK:
            BATCH_RESULT.clear()
            BATCH_RESULT.update(d)
            # unread=False 表示这份结果用户已经看过了：重启后还能查明细，
            # 但不要再次弹窗、更不要把已处理过的冲突重新合并回来
            BATCH_STATE["unread"] = bool(d.get("unread", True))
            # 统计也要回填，否则页头会显示「共 0 个 / 补全 0 / 失败 0」
            BATCH_STATE.update(
                total=d.get("total", 0),
                filled=d.get("filled", 0),
                conflicts=len(d.get("conflicts") or []),
                failed=len(d.get("failed") or []),
                finished=d.get("finished", ""),
            )


def normalize_batch_options(data):
    """把前端传来的批量设置收敛成可信值（非法值一律回落到默认，别让坏参数把整批跑空）。

    fields     要写哪些字段，默认全写（一个都没选也当全写：跑一遍什么都不写没意义）
    skip_status 哪些状态的账号不用查（默认 禁言/封号 + 注销）
    order      按什么排序（默认 added = 新下载的在前）
    """
    data = data if isinstance(data, dict) else {}
    raw_fields = data.get("fields")
    if isinstance(raw_fields, list):
        fields = [f for f in BATCH_FIELDS if f in raw_fields]
        if not fields:
            fields = list(BATCH_FIELDS)
    else:
        fields = list(BATCH_FIELDS)

    raw_skip = data.get("skip_status")
    if isinstance(raw_skip, list):
        skip = [s for s in raw_skip if s in BATCH_SKIPPABLE]
    else:
        skip = list(BATCH_SKIP_DEFAULT)

    order = str(data.get("order") or "added").strip()
    if order not in BATCH_ORDERS:
        order = "added"
    return {"fields": fields, "skip_status": skip, "order": order}


def build_batch_targets(opts):
    """按设置挑出要查的账号并排好序，返回 (targets, skipped)。

    skipped = 因为状态被跳过的数量（不进 failed：那是「查了没查到」，这是「压根没去查」）。
    """
    load_data()
    accounts = CACHE.get("accounts") or {}
    index = CACHE.get("list_index") or {}
    skip = set(opts.get("skip_status") or ())
    key, desc = BATCH_ORDERS.get(opts.get("order") or "added", BATCH_ORDERS["added"])

    rows = []
    skipped = 0
    for uid, a in accounts.items():
        if not isinstance(a, dict):
            continue
        st = str(a.get("status") or "").strip()
        if st and st in skip:
            skipped += 1
            continue
        stat = a.get("stats") or {}
        it = index.get(uid) or {}
        sort_val = {
            "added_at": it.get("added_at") or 0,
            "work_count": stat.get("work_count") or 0,
            "latest": stat.get("latest") or "",
            "earliest": stat.get("earliest") or "",
            "nickname": str(a.get("nickname") or ""),
        }.get(key, 0)
        rows.append((uid, a, sort_val))

    # 没有 sec_uid 的恒排最后：查不了，先去榨能查的（它们仍会进 failed，用户能看到）。
    # 用两次稳定排序：先按值排，再按「有没有 sec_uid」分组——这样 reverse 只作用于值，
    # 不会把「无 sec_uid」反过来顶到最前面。
    rows.sort(key=lambda r: r[2], reverse=desc)
    rows.sort(key=lambda r: not str((r[1] or {}).get("sec_uid") or "").strip())
    # 按「添加时间」排时，added_at=0 是「读不到文件夹创建时间」（合成账号 / 文件夹已删），
    # 不是「最旧」。升序时它会被顶到最前面，看着像「这些是刚加的」，所以单独压到最后。
    if key == "added_at":
        rows.sort(key=lambda r: not (r[2] or 0))
    return [(uid, a) for uid, a, _ in rows], skipped


def batch_profile_worker(options=None):
    """批量获取资料的后台线程（为什么放服务端跑，见 BATCH_STATE 注释）。

    规则（和详情页一致，别改成自动覆盖）：
    - 本地为空、抖音有值的字段 → 直接补（抖音号 / 简介）；
    - 本地有值且不一样的昵称 / 抖音号 → 一律不落盘，收进 conflicts 等用户逐条决定；
    - 每 25 个账号合并写一次 user_data.json（走 merge_save_user_data，带锁 + 原子写），
      中途被打断也只会丢最后一小段。
    """
    patches = {}
    conflicts = []
    failed = []
    filled_items = []      # 逐个记录补全了谁（点统计条「补全 N」时能看明细）
    filled = 0
    total = 0
    skipped = 0
    error = ""
    opts = normalize_batch_options(options)
    fields = opts["fields"]
    try:
        targets, skipped = build_batch_targets(opts)
        total = len(targets)
        with _BATCH_LOCK:
            BATCH_STATE.update(running=True, total=total, done=0, filled=0,
                               conflicts=0, failed=0, skipped=skipped, stop=False, error="",
                               options=opts,
                               started=time.strftime("%Y-%m-%d %H:%M:%S"), finished="")
        consec_fail = 0          # 连续失败计数：被限流时及时停下，别把整批跑成失败
        for i, (uid, a) in enumerate(targets, 1):
            with _BATCH_LOCK:
                if BATCH_STATE.get("stop"):
                    error = "已手动停止"
                    break
            if not isinstance(a, dict):
                continue
            try:
                r = fetch_profile_by_sec_uid(str(a.get("sec_uid") or ""), uid)
            except BaseException as e:            # 单个账号出问题不能拖死整批
                r = {"ok": False, "error": str(e)}
            if not r.get("ok") or (not r.get("nickname") and not r.get("douyin_id")):
                failed.append({"uid": uid,
                               "nickname": a.get("nickname") or "",
                               "error": (r.get("error") or "接口没返回数据")[:80]})
                consec_fail += 1
                if consec_fail >= 25:
                    error = ("连续 %d 个账号都取不到资料，多半是被抖音限流了，先停在这里；"
                             "过一会儿再点「开始获取」会接着跑（已处理的不会重复）" % consec_fail)
                    with _BATCH_LOCK:
                        BATCH_STATE["error"] = error
                    break
            else:
                consec_fail = 0
                cur_nick = str(a.get("nickname") or "").strip()
                cur_id = str(a.get("douyin_id") or "").strip()
                cur_sig = str(a.get("signature") or "").strip()
                new_nick = str(r.get("nickname") or "").strip()
                new_id = str(r.get("douyin_id") or "").strip()
                new_sig = str(r.get("signature") or "")
                nick_diff = bool(new_nick and cur_nick and new_nick != cur_nick)
                id_diff = bool(new_id and cur_id and new_id != cur_id)
                # 昵称对不上、抖音号也对不上（本地为空或不同）→ 很可能这个 SEC_UID 已经指向
                # 别的账号了。这时连「本地为空」的简介都不能写：2026-10-27 实测本地
                # 「不想泪眼萌萌」查回来是「困鱼」，简介被换成了别人的话。宁可整条不动，
                # 等用户确认（前端「全部采用」时才连同简介一起写）。
                # 抖音号能对上（本地有值且相同）= 同一账号只是改名了，可以放心补简介。
                same_account = bool(cur_id and new_id and cur_id == new_id)
                suspect = bool(nick_diff and not same_account)
                # 有备注（alias）的账号：列表和详情显示的都是备注，昵称怎么变都不影响识别，
                # 所以昵称差异直接采用，不再打扰用户（alias 本身永远不动，这里也不写它）。
                has_alias = bool(str(a.get("alias") or "").strip())
                if not suspect:
                    patch = {}
                    # 只写用户勾选的字段（BATCH_FIELDS）。判断逻辑不受影响：
                    # 「是不是同一个人」照样用抖音返回的昵称/抖音号比对，只是不落盘。
                    if "douyin_id" in fields and new_id and not cur_id:
                        patch["douyin_id"] = new_id
                    if "signature" in fields and new_sig.strip() and new_sig.strip() != cur_sig:
                        patch["signature"] = new_sig
                    if "nickname" in fields and has_alias and new_nick and new_nick != cur_nick:
                        patch["nickname"] = new_nick
                    if patch:
                        patches[uid] = patch
                        a.update(patch)           # 同步内存，后面的判断不吃旧值
                        filled += 1
                        filled_items.append({
                            "uid": uid,
                            "nickname": new_nick or cur_nick,
                            "douyin_id": new_id or cur_id,
                            # 只记改了哪几个字段，不存值（简介可能很长，全存会把结果文件撑大）
                            "changed": [k for k in ("nickname", "douyin_id", "signature") if k in patch],
                        })
                # 有备注的账号：昵称换不换对外都没影响（显示的是备注），所以昵称这一项
                # 不摆给用户决策；但「存疑」（认不出是不是同一个人）仍然要提示，
                # 只是卡片上不再显示昵称差异这一行。
                # 2026-10-28：收录条件跟着「本次获取哪几个字段」走 —— 用户没勾昵称，
                # 就不该拿昵称差异去烦他；但 suspect（整条一个字都没写）无条件收录，
                # 否则数据会悄悄地停在那里没人知道。
                nick_flag = bool(nick_diff and not has_alias and "nickname" in fields)
                id_flag = bool(id_diff and "douyin_id" in fields)
                if nick_flag or id_flag or suspect:
                    conflicts.append({
                        "uid": uid,
                        "nickname": cur_nick, "douyin_id": cur_id,
                        "newNickname": new_nick, "newDouyinId": new_id,
                        "newSignature": new_sig,
                        "nickDiff": nick_diff, "idDiff": id_diff,
                        "suspect": suspect, "hasAlias": has_alias,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    })
            if patches and i % 25 == 0:
                merge_save_user_data({"accounts": patches})
                patches = {}
            with _BATCH_LOCK:
                BATCH_STATE.update(done=i, filled=filled,
                                   conflicts=len(conflicts), failed=len(failed))
            time.sleep(0.15)
    except BaseException as e:
        error = "批量任务异常：" + str(e)
    finally:
        try:
            if patches:
                merge_save_user_data({"accounts": patches})
            load_user_data(force=True)
        except BaseException:
            pass
        result = {
            "conflicts": conflicts, "failed": failed, "filledList": filled_items,
            "filled": filled, "total": total, "skipped": skipped, "options": opts,
            "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with _BATCH_LOCK:
            BATCH_RESULT.clear()
            BATCH_RESULT.update(result)
            BATCH_STATE.update(running=False, unread=True, error=error, skipped=skipped,
                               conflicts=len(conflicts), failed=len(failed),
                               filled=filled, finished=result["finished"])
        _batch_result_save(result)


def fetch_profile_public(sec_uid):
    """公开接口兜底：昵称 / 抖音号能拿到，简介基本是空串。"""
    import urllib.request
    sec_uid = norm_sec_uid(sec_uid) or str(sec_uid or "").strip()
    if not sec_uid:
        return {"ok": False, "error": "没能从输入里认出 SEC_UID（v.douyin.com 短链不支持，请用 douyin.com/user/... 的主页地址）"}
    url = "https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid=" + urllib.parse.quote(sec_uid)
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Referer": "https://www.douyin.com/",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"ok": False, "error": "请求失败: %s" % e}
    if d.get("status_code") not in (0, None):
        return {"ok": False, "error": "接口返回 status_code=%s" % d.get("status_code")}
    u = d.get("user_info") or {}
    if not u:
        return {"ok": False, "error": "没有拿到资料"}
    return {
        "ok": True,
        "nickname": u.get("nickname") or "",
        "douyin_id": u.get("unique_id") or u.get("short_id") or "",
        "signature": u.get("signature") or "",
        "sec_uid": u.get("sec_uid") or sec_uid,
        "follower_count": u.get("follower_count"),
        "aweme_count": u.get("aweme_count"),
    }


def say(msg=""):
    """控制台输出一律立即刷新。

    背景：double-click 启动时窗口里只看到一行「正在载入索引...」然后长时间没动静，
    看起来就像「双击没反应」。所有启动期提示都走这里，保证一行行实时冒出来。
    """
    print(msg, flush=True)


def _instance_alive():
    """8723 上应答的是不是本程序（而不是别的软件偶然占了这个端口）。

    注意：Windows 上 `SO_REUSEADDR` 允许**两个进程同时 bind 同一端口**，所以
    「bind 成功」根本不能证明端口是空的——必须真发一个请求问一句。

    **必须绕开 HTTP_PROXY**：2026-10-30 查了很久的「服务反复消失 / 页面时好时坏」，
    根子是这台机器的环境里有 HTTP_PROXY=127.0.0.1:60105，urllib 连 127.0.0.1 也会走代理，
    代理回 502 → 这里判定「没在跑」→ 每次双击 bat 都再起一个实例，好几个进程抢同一个端口，
    请求随机落到没载入完的那个上（表现成页面卡住、删东西一直不结束）。
    """
    try:
        # 本机自检，绝不能经过任何代理
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:%d/api/meta" % PORT, timeout=2) as r:
            # 读全量而不是前 200 字节：以前靠「前 200 字节里有没有 total_authors」判断，
            # 一旦 /api/meta 的键顺序变了（或前面多塞了统计字段）就会误判成「没在跑」，
            # 于是又起一个实例去抢端口（2026-11-06 改成认固定标识 app）
            body = r.read().decode("utf-8", "replace")
            return '"douyin-local-library"' in body or '"total_authors"' in body
    except Exception:
        return False


def open_browser():
    """打开浏览器；打不开也要把地址打出来，别让用户对着黑窗口猜。"""
    url = f"http://127.0.0.1:{PORT}/"
    try:
        import webbrowser
        ok = webbrowser.open(url)
        if not ok:
            say(f"（没能自动拉起浏览器，请手动打开 {url}）")
    except Exception as e:
        say(f"（自动打开浏览器失败：{e}，请手动打开 {url}）")


def douyin_query_uids(sec_list):
    """批量 sec_uid -> {uid, nickname, unique_id}（走登录态 im/user/info 接口）。

    下载器 lib 里的 curl_cffi 是 3.12 编译版，服务端是 3.13，所以用 3.12 子进程跑
    query_uid_by_secuid.py。返回 {sec_uid: info}；失败/无 cookie 返回 {} 或含 __error。
    """
    if not sec_list:
        return {}
    if not (os.path.isfile(PYTHON312) and os.path.isfile(HELPER_QUERY_UID)):
        return {"__error": "找不到 Python312 或查询助手脚本"}
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        p = subprocess.run(
            [PYTHON312, HELPER_QUERY_UID], env=env,
            input=json.dumps({"sec_uids": sec_list}).encode("utf-8"),
            capture_output=True, timeout=90, cwd=SCRIPT_DIR)
        out = p.stdout.decode("utf-8", "replace")
        for line in reversed(out.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
    except Exception as e:
        print("[douyin] 查询 uid 失败:", e)
    return {"__error": "查询进程无输出"}


class ReuseHTTPServer(ThreadingHTTPServer):
    # **不能用 SO_REUSEADDR**：Windows 上它允许第二个进程绑同一个端口（两个实例一起抢，
    # 请求随机落到其中一个），这正是「页面时好时坏、删东西不结束、服务莫名其妙没了」的根源。
    # 改成 SO_EXCLUSIVEADDRUSE（见 server_bind）：端口被占就直接绑不上，走「端口被占用」分支。
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if os.name == "nt":
            import socket as _socket
            try:
                self.socket.setsockopt(_socket.SOL_SOCKET, _socket.SO_EXCLUSIVEADDRUSE, 1)
            except (OSError, AttributeError):
                pass
        super().server_bind()

    # 客户端（浏览器刷新 / 快速切页 / 关标签）提前断开是常态，socketserver 默认会把
    # 整个 traceback 打到 stderr。实测挂一天能刷出 2.6MB 日志，把后台任务的输出管道
    # 写爆后进程会异常退出（2026-10-28 那次服务「跑着跑着没了」就是这个）。
    # 这类错误没有可处理的东西（连接已经没了），直接静默；其他异常照旧打印。
    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    # 检查 data.json 是否存在，不存在则先扫描
    if not os.path.exists(DATA_JSON):
        say("首次运行，先扫描目录生成 data.json...（约 20 分钟）")
        try:
            subprocess.run([PYTHON, SCANNER_PY], cwd=SCRIPT_DIR)
        except OSError as e:
            # 双击 bat 时窗口一闪而过最常就是这个原因（PYTHON 路径写错/换机器没改）
            say("[错误] 扫描进程起不来：%s" % e)
            say("       检查 src/paths.py 里的 PYTHON 和 SCANNER_PY 是否存在。")
            say("       服务仍会启动（页面里是 0 位作者），改好后点「⟳ 重新扫描」。")

    # 先探测：端口上已经有本程序在跑吗？
    # Windows 的 SO_REUSEADDR 允许重复 bind，「try 一下失败就说明被占用」在这里是错的：
    # 第二次双击会静默起出第二个实例，请求随机落到其中一个，表现成「改了没生效」。
    # 所以先真发一个请求问一句，活着就别起第二份。
    if _instance_alive():
        say(f"本程序已在运行（端口 {PORT}），直接打开浏览器，不重复启动。")
        say("（若浏览器打开是空白页，说明那个实例已经不健康：任务管理器结束 python.exe 再双击）")
        open_browser()
        return

    try:
        server = ReuseHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        # 走到这里通常是别的软件占着端口（本机另有一个「推特下载浏览器」常驻在 8724）
        say(f"[错误] 端口 {PORT} 被其他程序占用，服务无法启动。")
        say(f"       原因：{e}")
        say(f"       办法：关掉占用 {PORT} 的程序，或改 src/paths.py 里的 PORT。")
        return

    # 启动时预载入数据到内存，避免首次请求等待。每一步都打出来，
    # 否则 74MB 索引要等十几秒，窗口看起来像卡死。
    say("整理封面缓存...")
    t_mig = time.time()
    migrate_cover_cache()          # 把历史封面从 thumb_cache 搬到 cover_cache
    say(f"  封面缓存就绪（{time.time() - t_mig:.2f} 秒）")
    say("正在载入索引...（几十 MB，通常一两秒）")
    t0 = time.time()
    load_data(force=True)
    ls = CACHE.get("load_stat") or {}
    say(f"索引就绪，用时 {time.time() - t0:.1f} 秒 · {len(CACHE['list'])} 位作者"
        f"（添加时间：缓存命中 {ls.get('hit', 0)} / 读盘 {ls.get('stat', 0)} 个、{ls.get('sec', 0)} 秒）")
    if ls.get("sec", 0) > 5:
        say("  ↑ 这一步在读 E: 机械盘上 1700 个作者文件夹的创建时间，盘刚唤醒时很慢。")
        say("    已经写进 data/added_at_cache.json，下次启动走缓存，不再读盘。")
    # 上次批量获取的结果没被前端取走 → 读回来，打开页面时会自动提示
    _batch_result_load()
    say("")
    say(f"服务已启动: http://127.0.0.1:{PORT}/")
    say(f"媒体根目录: {SCAN_ROOT}")
    say("关闭这个窗口即停止服务；运行期间请勿关闭")

    # 事件监听：装在下载根目录上，DouK 一落盘系统就通知我们（不轮询、不主动问盘）
    global WATCHER
    try:
        import watcher as _watcher_mod
        WATCHER = _watcher_mod.Watcher(SCAN_ROOT, log=lambda m: print(m, flush=True))
        WATCHER.start()
        time.sleep(0.3)
        if WATCHER.alive:
            say("后台更新：监听下载目录中（DouK 一落盘就自动收录，不轮询、不空转硬盘）")
        else:
            say("后台更新：监听没起来（%s）→ 退回每 %d 分钟查一次"
                % (WATCHER.error or "未知原因", AUTO_SCAN_INTERVAL // 60))
            WATCHER = None
    except Exception as e:
        print("[watch] 启动失败：%s" % e, flush=True)
        WATCHER = None
    if WATCHER is None:
        say(f"后台增量扫描：每 {AUTO_SCAN_INTERVAL // 60} 分钟看一次有没有新下载，有就自己扫（只扫变了的文件夹，1~2 秒）")
    say("")

    if WATCHER is not None:
        threading.Thread(target=watch_loop, daemon=True).start()
    else:
        threading.Thread(target=auto_scan_loop, daemon=True).start()
    threading.Timer(0.8, open_browser).start()
    threading.Thread(target=_watch_autostart, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
