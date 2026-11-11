# -*- coding: utf-8 -*-
"""
抖音作者标记 · 本地只读查询服务
================================
只做三件事：
  1. 以「只读」方式打开本地已有的 download_log.db 与 user_data.json
  2. 在内存里建一份 uid/sec_uid/昵称 索引
  3. 对浏览器扩展提供 127.0.0.1 上的极简 JSON 接口

不写库、不改任何文件、不联外网、不上传任何数据。
端口默认 8790，仅绑定 127.0.0.1（外网/局域网访问不到）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.3.9"
BASE = Path(__file__).resolve().parent
# 浏览器扩展的请求来源前缀（MV3 service worker 里 fetch 127.0.0.1 时带的就是它）。
# 只认这一类来源；普通网页（https://任意站）一律拒绝，见 Handler._client_ok。
EXT_ORIGIN_PREFIX = "chrome-extension://"
AWEME_RE = re.compile(r"(?<!\d)(\d{18,20})(?!\d)")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-?")
EXT_RE = re.compile(r"\.(mp4|mov|webm|jpe?g|png|webp|gif|heic)$", re.I)
TAIL_ID_RE = re.compile(r"-\d{15,20}$")
IMG_IDX_RE = re.compile(r"_\d+$")
STRIP_RE = re.compile(r"[\\/:*?\"<>|\s\u200b\ufeff・·．.]+")

DEFAULT_CFG = {
    "port": 8790,
    "bind": "127.0.0.1",
    # 数据来源：默认指向同盘的「browser/data」目录
    "download_db": r"D:\ai_Project\douyin-local-library\browser\data\download_log.db",
    "user_data": r"D:\ai_Project\douyin-local-library\browser\data\user_data.json",
    "remote_latest": r"D:\ai_Project\douyin-local-library\browser\data\remote_latest.json",
    "admin_base": "http://127.0.0.1:8723",
    "queue_file": "pending-queue.json",
    "recent_per_author": 12,      # 每位作者保留的最近下载记录条数
    "build_aweme_index": True,    # 建「作品ID → 下载时间」索引（用于判断当前视频是否已下载）
    "auto_reload": True,          # 数据文件变了就自动重建索引（每 60 秒检查一次）
    "verbose": True,
}


def log(*a):
    if CFG.get("verbose"):
        try:
            print(time.strftime("[%H:%M:%S]"), *a, flush=True)
        except Exception:
            pass


def load_cfg() -> dict:
    cfg = dict(DEFAULT_CFG)
    p = BASE / "config.json"
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text("utf-8")))
        except Exception as e:
            log("config.json 读取失败，用默认值：", e)
    # 环境变量可覆盖端口，方便调试
    if os.environ.get("DY_BRIDGE_PORT"):
        cfg["port"] = int(os.environ["DY_BRIDGE_PORT"])
    return cfg


CFG = load_cfg()


def norm_name(s: str) -> str:
    return (s or "").strip().casefold()


def _to_int(v, default=0) -> int:
    """宽容地把请求里的值转成整数：`"12"` / `12.0` / `None` / `"abc"` 都不抛异常。

    这些值来自扩展（也来自任何能访问 8790 的东西），不能假设类型。
    """
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def desc_key(desc: str) -> str:
    """文案归一化后取前 20 字，用来和文件名里的 desc 段对齐。"""
    return STRIP_RE.sub("", desc or "")[:20].lower()


def split_filename(name: str):
    """'2026-10-13-示例昵称-示例描述.mp4' -> ('2026-10-13', '归一化尾巴')

    文件名格式：YYYY-MM-DD-昵称-desc[-作品ID][_图序].ext
    昵称里也可能有 '-'，所以不硬切昵称，只取「日期」+「归一化后的整个尾巴」，
    匹配时用 desc_key 是否包含在尾巴里判断。
    """
    base = EXT_RE.sub("", name or "")
    m = DATE_RE.match(base)
    if not m:
        return "", ""
    tail = base[m.end():]
    tail = TAIL_ID_RE.sub("", tail)
    tail = IMG_IDX_RE.sub("", tail)
    return m.group(1), STRIP_RE.sub("", tail).lower()


def open_ro(path: str):
    """只读打开 sqlite；文件不存在返回 None。"""
    if not Path(path).exists():
        return None
    uri = "file:" + urllib.parse.quote(path.replace("\\", "/")) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        log("打开数据库失败：", e)
        return None


class AuthorIndex:
    """全量索引，进程内常驻；重建一次 1~3 秒，之后查询是纯内存命中。"""

    def __init__(self):
        self.lock = threading.RLock()
        self.authors: dict[str, dict] = {}     # uid -> 记录
        self.by_sec: dict[str, str] = {}       # sec_uid -> uid
        self.by_nick: dict[str, list[str]] = {}  # 规范化昵称 -> [uid]
        self.aweme: dict[str, tuple[str, int, int]] = {}  # 作品ID -> (uid, mtime, size)
        self.folders: dict[str, str] = {}      # uid -> 文件夹名（用于按前缀扫文件）
        self.remote: dict[str, dict] = {}      # uid -> {pub, crawl} 抖音上最新发布（下载器作品表快照）
        self.live: dict[str, dict] = {}        # uid -> {latest, at}   联网查到的真实发布时间（优先）
        self._ufiles: "OrderedDict[str, list]" = OrderedDict()  # 作者文件清单缓存(LRU)
        self._uf_lock = threading.RLock()
        self.indexed_at = 0.0
        self.scanned_at = ""
        self.total_files = 0
        self.build_ms = 0

    # ---------- 构建 ----------
    def rebuild(self) -> dict:
        t0 = time.time()
        with self.lock:
            self._build()
            self.build_ms = int((time.time() - t0) * 1000)
            self.indexed_at = time.time()
        log(f"索引完成：{len(self.authors)} 位作者 / {self.total_files} 个文件 / "
            f"{len(self.aweme)} 个作品ID / {self.build_ms} ms")
        return self.summary()

    def _build(self):
        authors: dict[str, dict] = {}
        by_sec: dict[str, str] = {}
        by_nick: dict[str, list[str]] = {}

        # 1) user_data.json：昵称、抖音号、sec_uid、星标、状态
        up = CFG["user_data"]
        if Path(up).exists():
            try:
                raw = json.loads(Path(up).read_text("utf-8"))
                accs = raw.get("accounts") or {}
                for uid, a in accs.items():
                    uid = str(uid)
                    rec = {
                        "uid": uid,
                        "nickname": (a.get("nickname") or "").strip(),
                        "douyin_id": (a.get("douyin_id") or "").strip(),
                        "sec_uid": (a.get("sec_uid") or "").strip(),
                        "starred": bool(a.get("starred")),
                        "status": (a.get("status") or "").strip(),
                        "level": (a.get("level") or "").strip(),
                        "update_freq": (a.get("update_freq") or "").strip(),
                        "alias": (a.get("alias") or "").strip(),
                        "tags": [str(t) for t in (a.get("tags") or []) if str(t).strip()],
                        "file_count": 0, "bytes": 0,
                        "last_download": 0, "first_download": 0,
                        "recent": [],
                    }
                    authors[uid] = rec
                    if rec["sec_uid"]:
                        by_sec[rec["sec_uid"]] = uid
                    for alt in (a.get("alt_accounts") or []):
                        alt = str(alt)
                        if alt and alt not in authors:
                            authors[alt] = dict(rec, uid=alt, alt_of=uid)
            except Exception as e:
                log("user_data.json 解析失败：", e)
        else:
            log("找不到 user_data.json：", up)

        # 2) download_log.db：作品数 / 体积 / 最后下载时间 / 最近若干条
        conn = open_ro(CFG["download_db"])
        if conn is None:
            log("找不到 download_log.db：", CFG["download_db"])
        else:
            try:
                keep = int(CFG["recent_per_author"])
                folders: dict[str, str] = {}
                for r in conn.execute(
                    "SELECT uid, folder, file_count, bytes, last_download FROM author"
                ):
                    uid = str(r["uid"])
                    rec = authors.get(uid)
                    if rec is None:
                        # 有文件夹但 user_data 里没有（手工建的），也要能命中
                        rec = {"uid": uid, "nickname": "", "douyin_id": "", "sec_uid": "",
                               "starred": False, "status": "", "level": "",
                               "file_count": 0, "bytes": 0, "last_download": 0,
                               "first_download": 0, "recent": []}
                        nm = (r["folder"] or "")
                        m = re.match(r"UID\d+_(.*?)_发布作品$", nm)
                        if m:
                            rec["nickname"] = m.group(1)
                        authors[uid] = rec
                    rec["file_count"] = int(r["file_count"] or 0)
                    rec["bytes"] = int(r["bytes"] or 0)
                    rec["last_download"] = int(r["last_download"] or 0)
                    rec["folder"] = r["folder"] or ""
                    folders[uid] = rec["folder"]

                # 最近 N 条下载记录：一次全表扫描，按时间倒序保留每人前 N 条
                n = 0
                recent: dict[str, list] = {}
                aweme: dict[str, tuple[str, int]] = {}
                build_aweme = bool(CFG["build_aweme_index"])
                for r in conn.execute(
                    "SELECT uid, name, mtime, size FROM file ORDER BY mtime DESC"
                ):
                    n += 1
                    uid = str(r["uid"])
                    lst = recent.get(uid)
                    if lst is None:
                        lst = recent[uid] = []
                    if len(lst) < keep:
                        lst.append({"name": r["name"], "mtime": int(r["mtime"] or 0),
                                    "size": int(r["size"] or 0)})
                    if build_aweme:
                        mm = AWEME_RE.search(r["name"] or "")
                        if mm:
                            aweme.setdefault(mm.group(1),
                                             (uid, int(r["mtime"] or 0), int(r["size"] or 0)))
                self.total_files = n
                self.folders = folders
                for uid, lst in recent.items():
                    rec = authors.get(uid)
                    if rec is not None:
                        rec["recent"] = lst
                # 最早一次下载（真实 MIN，不是「最近N条」里的最小值）
                for r in conn.execute(
                    "SELECT uid, MIN(mtime) AS mn, COUNT(*) AS c FROM file GROUP BY uid"
                ):
                    rec = authors.get(str(r["uid"]))
                    if rec is not None:
                        rec["first_download"] = int(r["mn"] or 0)
                        if not rec.get("file_count"):
                            rec["file_count"] = int(r["c"] or 0)
                self.aweme = aweme
                try:
                    row = conn.execute("SELECT MAX(last_scan) FROM author").fetchone()
                    self.scanned_at = (row[0] or "") if row else ""
                except Exception:
                    self.scanned_at = ""
            finally:
                conn.close()

        # 3) 昵称索引（含历史改名，用户搜旧名也能命中）
        for uid, rec in authors.items():
            for nm in filter(None, [rec.get("nickname"), rec.get("alias")]):
                k = norm_name(nm)
                if not k:
                    continue
                lst = by_nick.setdefault(k, [])
                if uid not in lst:
                    lst.append(uid)
        # 主账号已存在同名时，去掉 alt 里的重复项
        for k, lst in list(by_nick.items()):
            prim = [u for u in lst if not authors.get(u, {}).get("alt_of")]
            if prim:
                by_nick[k] = prim

        # 4) 抖音上「该作者最新发布」缓存（管理后台爬的），用来算本地落后几天
        remote: dict[str, dict] = {}
        rp = CFG.get("remote_latest") or ""
        if rp and Path(rp).exists():
            try:
                raw = json.loads(Path(rp).read_text("utf-8"))
                for uid, v in (raw.get("authors") or {}).items():
                    if isinstance(v, dict) and (v.get("pub") or v.get("crawl")):
                        remote[str(uid)] = {"pub": v.get("pub") or "", "crawl": v.get("crawl") or ""}
            except Exception as e:
                log("remote_latest.json 读取失败：", e)
        self.remote = remote

        # 4b) 联网查到的「抖音最新作品发布时间」（网页端 /api/latest_work 写的同一份缓存）。
        #     remote_latest.json 是**下载器作品表 xlsx 的历史快照**，可能停留在一年前：
        #     拿它算「本地是否跟上」会得出「抖音最新 2025-08-27 · 本地已跟上(2025-08-31 查)」
        #     这种明显倒挂的结论（2026-11-06 用户反馈）。有联网数据就一定先用它。
        live: dict[str, dict] = {}
        lp = (Path(rp).parent / "latest_work.json") if rp else None
        if lp and lp.exists():
            try:
                raw = json.loads(lp.read_text("utf-8"))
                for uid, v in (raw.get("authors") or {}).items():
                    latest = (v or {}).get("latest") or ""
                    if latest:
                        live[str(uid)] = {"latest": str(latest), "at": str(v.get("at") or "")}
            except Exception as e:
                log("latest_work.json 读取失败：", e)
        self.live = live

        # 4c) 追更名单（browser/data/watch.json）：网页上要标「正在追更」（2026-11-10）。
        #     和 user_data.json 同目录，每次重建索引读一遍（几百条，几毫秒）。
        #     按 uid 认人，跟后台 /api/authors 的 watch 判定完全同一套口径。
        wpath = Path(CFG["user_data"]).parent / "watch.json"
        wuids: set = set()
        if wpath.exists():
            try:
                raw = json.loads(wpath.read_text("utf-8"))
                for x in (raw.get("authors") or []):
                    u = str((x or {}).get("uid") or "").strip()
                    if u:
                        wuids.add(u)
            except Exception as e:
                log("watch.json 读取失败（追更标记会全灭，其它不受影响）：", e)
        else:
            log("找不到 watch.json：", str(wpath))
        for uid, rec in authors.items():
            rec["watch"] = uid in wuids
        self.watch_count = len(wuids)

        # 文件清单缓存要作废（索引重建了）
        with self._uf_lock:
            self._ufiles.clear()

        self.authors = authors
        self.by_sec = by_sec
        self.by_nick = by_nick

    # ---------- 查询 ----------
    def summary(self) -> dict:
        return {
            "authors": len(self.authors),
            "files": self.total_files,
            "aweme_ids": len(self.aweme),
            "scanned_at": self.scanned_at,
            "build_ms": self.build_ms,
            "indexed_at": int(self.indexed_at),
        }

    def _files_of(self, uid: str) -> list:
        """某位作者的全部文件（日期, 归一化尾巴, 文件名, 大小, 时间）。按 uid 缓存，LRU 80。"""
        with self._uf_lock:
            got = self._ufiles.get(uid)
            if got is not None:
                self._ufiles.move_to_end(uid)
                return got
        rows = []
        conn = open_ro(CFG["download_db"])
        if conn is not None:
            try:
                prefix = self.folders.get(uid) or ("UID" + str(uid) + "_")
                if any(ch in prefix for ch in "*?["):
                    # 昵称里带通配符：退回 LIKE 精确前缀
                    esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    cur = conn.execute(
                        "SELECT name, size, mtime FROM file WHERE rel LIKE ? ESCAPE '\\'",
                        (esc + "%",))
                else:
                    cur = conn.execute(
                        "SELECT name, size, mtime FROM file WHERE rel GLOB ?", (prefix + "*",))
                for r in cur:
                    d, tail = split_filename(r["name"])
                    if d:
                        rows.append((d, tail, r["name"], int(r["size"] or 0), int(r["mtime"] or 0)))
            except Exception as e:
                log("读作者文件失败：", uid, e)
            finally:
                conn.close()
        with self._uf_lock:
            self._ufiles[uid] = rows
            while len(self._ufiles) > 80:
                self._ufiles.popitem(last=False)
        return rows

    def video_info(self, uid: str, aweme_id: str, desc: str, create_ts: int) -> dict:
        """这个视频在本地有没有：先按作品ID，再按「发布日期 + 文案前20字」对文件名。"""
        out = {"aweme_id": str(aweme_id or ""), "downloaded": False, "how": ""}
        if aweme_id:
            hit = self.aweme.get(str(aweme_id).strip())
            if hit and (not uid or hit[0] == uid):
                out.update(downloaded=True, mtime=hit[1], uid=hit[0],
                           size=(hit[2] if len(hit) > 2 else 0), how="id")
                return out
        if not uid or not create_ts:
            return out
        try:
            date = time.strftime("%Y-%m-%d", time.localtime(int(create_ts)))
        except (ValueError, OSError, OverflowError):
            return out
        dk = desc_key(desc)
        if len(dk) < 4:
            # 太短的文案（一两个字）几乎能命中任何文件名，宁可不认
            return out
        # 硬约束：本地文件不可能比作品发布还早下载。差得离谱就直接判「没存」，
        # 免得把几个月前的旧作品认成这条新视频。
        earliest = int(create_ts) - 600
        best = None
        for item in self._files_of(uid):
            if item[0] != date:
                continue
            if item[4] < earliest:
                continue
            # 文件名里已经写着另一个作品 ID → 那肯定不是这条，别硬凑
            fid = AWEME_RE.search(item[2] or "")
            if fid and fid.group(1) != str(aweme_id or "").strip():
                continue
            if dk in item[1] and (best is None or item[4] < best[4]):
                best = item
        if best:
            out.update(downloaded=True, mtime=best[4], size=best[3], name=best[2], how="desc")
        return out

    def lookup(self, sec_uid: str = "", uid: str = "", nickname: str = "",
               aweme_id: str = "", meta: dict | None = None) -> dict:
        """认人只认确定性 ID：SEC_UID / uid / 作品ID。

        2026-11-03 起**彻底不再按昵称认人**（用户口径：「以后改为用 SEC_UID 判断作者」）。
        原因是昵称一点都不唯一：同名的一大把，同一个人还会改名、换号，
        以前「昵称精确 + 昵称包含」两条路把 `应翔专营店` 的新号接到了已封的旧档案上。
        现在昵称只用来**列提示**（库里同名的是谁），永不参与判定。

        三条线索：
          ① SEC_UID  页面 URL / 锚点 / 接口响应里的 MS4wLjAB…，终身不变，唯一权威
          ② uid      页面极少给；给了就是库主键，同样确定
          ③ 作品ID   作品归属是确定的，用它反查作者不算猜
        """
        with self.lock:
            matched_by, target = "none", None
            sec_q = (sec_uid or "").strip()
            uid_q = str(uid or "").strip()

            if sec_q:
                target = self.by_sec.get(sec_q)
                if target:
                    matched_by = "sec_uid"
            if not target and uid_q and uid_q in self.authors:
                target, matched_by = uid_q, "uid"
            if not target and aweme_id:
                # 页面只给了视频ID（推荐流常见）：库里这个作品是我们下载过的，就用它反查作者
                hit = self.aweme.get(str(aweme_id).strip())
                if hit:
                    target, matched_by = hit[0], "aweme"

            if not target:
                # 没认出来：把库里同名的那几条**只当提示**列出来，绝不当成本人。
                # （一旦塞进 author，扩展会拿它的 uid/sec_uid 去推「待下载」，就推错人了）
                # 分两种：
                #   ① 库里填了 SEC_UID 且都不是页面的 → 同名的另一个号，很常见
                #   ② 库里那条压根没填 SEC_UID     → 无从判断，只能说「无法确认」
                same: list[dict] = []
                unfilled: list[dict] = []
                if nickname:
                    for u in (self.by_nick.get(norm_name(nickname)) or [])[:8]:
                        r = self.authors.get(u) or {}
                        one = {"uid": u, "nickname": r.get("nickname") or "",
                               "douyin_id": r.get("douyin_id") or "",
                               "sec_uid": r.get("sec_uid") or "",
                               "status": r.get("status") or "",
                               "level": r.get("level") or "",
                               "file_count": r.get("file_count") or 0,
                               "last_download": r.get("last_download") or 0}
                        (same if one["sec_uid"].strip() else unfilled).append(one)

                # need_sec=True 表示「页面根本没拿到 SEC_UID」，前端据此提示「点进主页再查」
                need_sec = not sec_q

                if need_sec:
                    # 连页面都没给 SEC_UID：既证明不了是本人，也证明不了是另一个人。
                    # 只说「只认出昵称」，把同名的列出来当参考 —— 不下 name_diff 那种结论。
                    same_and_unfilled = same + unfilled
                    return {"status": "miss", "matched_by": "none", "need_sec": True,
                            "candidates": [x["uid"] for x in same_and_unfilled],
                            "same_name": same_and_unfilled}
                if same:
                    log("同名不同号：页面 sec=%s 昵称=%s；库里同名的是 %s（%s 个作品）" % (
                        sec_q[:20], nickname, same[0].get("nickname") or "",
                        same[0].get("file_count")))
                    return {"status": "name_diff", "matched_by": "sec_uid_mismatch",
                            "need_sec": need_sec,
                            "candidates": [x["uid"] for x in same],
                            "same_name": same, "same_unfilled": unfilled}
                if unfilled:
                    return {"status": "name_unfilled", "matched_by": "sec_uid_missing",
                            "need_sec": need_sec,
                            "candidates": [x["uid"] for x in unfilled],
                            "same_name": unfilled, "same_unfilled": unfilled}
                return {"status": "miss", "matched_by": "none", "need_sec": need_sec,
                        "candidates": []}

            rec = self.authors.get(target) or {}
            out = {
                "uid": rec.get("uid"),
                "nickname": rec.get("nickname") or "",
                "douyin_id": rec.get("douyin_id") or "",
                "sec_uid": rec.get("sec_uid") or "",
                "starred": bool(rec.get("starred")),
                "status": rec.get("status") or "",
                "level": rec.get("level") or "",
                "update_freq": rec.get("update_freq") or "",
                "watch": bool(rec.get("watch")),
                "alias": rec.get("alias") or "",
                "tags": rec.get("tags") or [],
                "file_count": rec.get("file_count") or 0,
                "bytes": rec.get("bytes") or 0,
                "last_download": rec.get("last_download") or 0,
                "first_download": rec.get("first_download") or 0,
                "recent": (rec.get("recent") or [])[:6],
                "alt_of": rec.get("alt_of") or "",
            }
            # 只认「同一个作品」的 meta。
            # 页面上没有作品 ID 时，扩展会带上「最近一次接口响应」的作者级信息，
            # 那可能是**别的视频**（甚至几个月前的）——拿它的 aweme_id / 发布日期
            # 去匹配，就会把一条一小时前才发的视频说成「已存」。宁可什么都不说。
            m = meta or {}
            qid = str(aweme_id or "").strip()
            mid = str(m.get("aweme_id") or "").strip()
            same_video = bool(qid) and bool(mid) and qid == mid
            if not same_video:
                m = {"au": m.get("au")} if m.get("au") else {}
            aweme_id = qid
            video = self.video_info(target, aweme_id, m.get("desc") or "",
                                    int(m.get("create_time") or 0)) if (aweme_id and same_video) else None
            # 抖音最新发布时间：**联网查到的优先**，旧的 xlsx 快照只在没联网查过时兜底。
            # 反过来的话就会出现「抖音最新 2025-08-27」这种一年前的倒挂（2026-11-06 修）。
            remote = None
            _live = self.live.get(target) or None
            if _live:
                remote = {"pub": str(_live["latest"])[:10],
                          "crawl": str(_live.get("at") or "")[:10],
                          "live": _live["latest"], "src": "live"}
            else:
                _old = self.remote.get(target) or None
                if _old:
                    remote = dict(_old, src="table")
            # 「最新作品」按**发布日**排，不按下载时间：批量下载时几十个文件的 mtime
            # 全挤在同一分钟，按 mtime 排会把旧作品顶到最上面（10-14 排在 10-19 前面）
            try:
                files = self._files_of(target)
                if files:
                    top = sorted(files, key=lambda x: (x[0], x[4]), reverse=True)[:3]
                    out["recent"] = [{"date": f[0], "name": f[2], "size": f[3], "mtime": f[4]}
                                     for f in top]
            except Exception as e:
                log("recent 按发布日重排失败：", e)
            return {"status": "hit", "matched_by": matched_by, "author": out,
                    "candidates": [], "video": video, "remote": remote}


IDX = AuthorIndex()


# ------------------------------------------------------------ 待下载区（队列）
class Queue:
    """浏览器插件 → 管理后台的「待下载区」。

    存在本地一个 JSON 里（就在本服务目录下，不碰管理后台的数据文件）。
    「清空」会把内容挪进 undo，可一键取消清空。
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.path = BASE / (CFG.get("queue_file") or "待下载区.json")
        self.data = {"items": [], "undo": [], "cleared_at": ""}
        self._load()

    @staticmethod
    def _same(a: dict, b: dict) -> bool:
        """判断两条是不是同一个人。

        只比 uid 不够：陌生作者推送时 uid 还是空的，建档成功后 uid 才有值，
        同一个人会被当成两个。所以 uid / sec_uid 任一对上就算重复；
        两个 ID 都没有时才退回比昵称。
        """
        ua = str(a.get("uid") or "").strip()
        ub = str(b.get("uid") or "").strip()
        if ua and ub and ua == ub:
            return True
        sa = str(a.get("sec_uid") or "").strip()
        sb = str(b.get("sec_uid") or "").strip()
        if sa and sb and sa == sb:
            return True
        if not ua and not ub and not sa and not sb:
            na = norm_name(a.get("nickname"))
            nb = norm_name(b.get("nickname"))
            return bool(na) and na == nb
        return False

    @classmethod
    def _dedup(cls, items: list) -> list:
        """同一个人的多条只留一条（items[0] 最新），并把旧条目的信息合并进来。

        典型场景：先推一次（uid 为空、created=True），建档后再推一次
        （uid 有了、created=False），两条其实是同一个人。
        """
        out = []
        for it in items:
            kept = None
            for cand in out:
                if cls._same(it, cand):
                    kept = cand
                    break
            if kept is None:
                out.append(it)
                continue
            # 用这一条补全「保留的那条」缺失的信息
            for k in ("uid", "sec_uid", "nickname", "douyin_id", "level", "status",
                      "follower", "signature", "avatar"):
                if not kept.get(k) and it.get(k):
                    kept[k] = it[k]
            if it.get("tags") and not kept.get("tags"):
                kept["tags"] = it["tags"]
            if it.get("created") and not kept.get("created"):
                kept["created"] = True
                kept["note"] = it.get("note") or kept.get("note") or ""
        return out

    def _load(self):
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text("utf-8"))
                if isinstance(d, dict):
                    raw_items = d.get("items") or []
                    raw_undo = d.get("undo") or []
                    self.data["items"] = self._dedup(raw_items)
                    self.data["undo"] = self._dedup(raw_undo)
                    self.data["cleared_at"] = d.get("cleared_at") or ""
                    if len(self.data["items"]) != len(raw_items):
                        log("待下载区：清理了 %d 条重复" % (len(raw_items) - len(self.data["items"])))
                        self._save()
        except Exception as e:
            log("待下载区读取失败：", e)

    def backfill(self, index) -> int:
        """把队列里缺的昵称/等级/状态/标签，用本地作者库补上。

        典型场景：推的时候这个人还不在库里、页面上也没认出昵称，
        只带着 sec_uid 进来的；后台建档完成后本地库里就有名字了，
        这里自动补上，列表不会再显示一串 uid。
        """
        if index is None or not getattr(index, "authors", None):
            return 0
        fixed = 0
        with self.lock:
            for it in self.data["items"]:
                uid = str(it.get("uid") or "").strip()
                sec = str(it.get("sec_uid") or "").strip()
                if not uid and sec:
                    uid = index.by_sec.get(sec) or ""
                    if uid:
                        it["uid"] = uid
                rec = index.authors.get(uid) if uid else None
                if not rec:
                    continue
                changed = False
                for k, src in (("nickname", "nickname"), ("douyin_id", "douyin_id"),
                               ("level", "level"), ("status", "status")):
                    if not (it.get(k) or "").strip() and (rec.get(src) or "").strip():
                        it[k] = rec[src]
                        changed = True
                if not it.get("tags") and rec.get("tags"):
                    it["tags"] = list(rec["tags"])
                    changed = True
                if not (it.get("signature") or "").strip() and (rec.get("signature") or "").strip():
                    it["signature"] = rec["signature"]
                    changed = True
                if changed:
                    fixed += 1
            if fixed:
                self._save()
        if fixed:
            log("待下载区：用本地库补全了 %d 条的昵称/资料" % fixed)
        return fixed

    def _save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            log("待下载区写入失败：", e)

    def add(self, item: dict) -> tuple:
        with self.lock:
            for it in self.data["items"]:
                if self._same(item, it):
                    # 已经在了：把新拿到的信息补进去（uid 往往是建档后才有）
                    changed = False
                    for k in ("uid", "sec_uid", "nickname", "douyin_id", "level", "status",
                              "follower", "signature", "avatar"):
                        if not it.get(k) and item.get(k):
                            it[k] = item[k]
                            changed = True
                    if item.get("tags") and not it.get("tags"):
                        it["tags"] = item["tags"]
                        changed = True
                    if item.get("created") and not it.get("created"):
                        it["created"] = True
                        it["note"] = item.get("note") or it.get("note") or ""
                        changed = True
                    if changed:
                        self._save()
                    return it, False          # 已在队列里
            item["added_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.data["items"].insert(0, item)
            self._save()
            return item, True

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "items": list(self.data["items"]),
                "count": len(self.data["items"]),
                "can_undo": bool(self.data["undo"]),
                "undo_count": len(self.data["undo"]),
                "cleared_at": self.data["cleared_at"],
            }

    def clear(self) -> dict:
        with self.lock:
            self.data["undo"] = list(self.data["items"])
            n = len(self.data["items"])
            self.data["items"] = []
            self.data["cleared_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._save()
            return {"cleared": n, **self.snapshot()}

    def remove(self, uid: str = "", sec_uid: str = "", nickname: str = "") -> dict:
        """从待下载区里删掉一条（按 uid / sec_uid / 昵称 依次匹配）。"""
        uid = str(uid or "").strip()
        sec = str(sec_uid or "").strip()
        nick = norm_name(nickname)
        if not (uid or sec or nick):
            return {"ok": False, "error": "缺少 uid / sec_uid / nickname", "removed": 0,
                    **self.snapshot()}
        with self.lock:
            keep, gone = [], []
            for it in self.data["items"]:
                iu = str(it.get("uid") or "").strip()
                isec = str(it.get("sec_uid") or "").strip()
                inn = norm_name(it.get("nickname"))
                hit = (uid and iu and iu == uid) or (sec and isec and isec == sec) \
                    or (not uid and not sec and nick and inn and inn == nick)
                (gone if hit else keep).append(it)
            if not gone:
                return {"ok": True, "removed": 0, "name": "", **self.snapshot()}
            self.data["items"] = keep
            self._save()
            first = gone[0]
            return {"ok": True, "removed": len(gone),
                    "name": first.get("nickname") or first.get("uid") or "",
                    **self.snapshot()}

    def undo_clear(self) -> dict:
        with self.lock:
            self.data["items"] = list(self.data["undo"]) + list(self.data["items"])
            self.data["undo"] = []
            self.data["cleared_at"] = ""
            self._save()
            return self.snapshot()


QUEUE = Queue()


def admin_create_author(sec_uid: str) -> dict:
    """调管理后台的 /api/create_author 建作者主页（它会自己查 UID、拉资料、写 user_data）。"""
    base = (CFG.get("admin_base") or "http://127.0.0.1:8723").rstrip("/")
    body = json.dumps({"sec_uid": sec_uid}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + "/api/create_author", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "后台返回 HTTP %s" % e.code}
    except Exception as e:
        return {"ok": False, "error": "连不上管理后台（8723 没开？）：%s" % e}


def _admin_origins() -> set:
    """管理后台页面的来源（它自己也会直接 fetch 8790 的 /queue）。

    `app.js` 里 `QUEUE_BASE = 'http://127.0.0.1:8790'`，是从 8723 的页面里跨源请求的，
    Origin 就是 `http://127.0.0.1:8723`。2026-11-06 收紧 CORS 时只放行了扩展，
    忘了这一条 → 待下载区直接显示「8790 没启动」（实际 403）。
    端口从配置的 admin_base 推，改了后台端口也能对上。
    """
    base = (CFG.get("admin_base") or "http://127.0.0.1:8723").strip().rstrip("/")
    try:
        port = urllib.parse.urlparse(base).port or 80
    except ValueError:
        port = 8723
    return {"http://127.0.0.1:%d" % port,
            "http://localhost:%d" % port,
            "http://[::1]:%d" % port}


# ---------------------------------------------------------------- HTTP 层
class Handler(BaseHTTPRequestHandler):
    server_version = "DyLocalBridge/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 安静模式，只记错误
        if args and str(args[1]) not in ("200", "204", "304"):
            log("%s %s" % (self.address_string(), fmt % args))

    def _cors(self):
        """只对「扩展」和「管理后台页面」回 CORS 头（2026-11-06 收紧）。

        以前是 `Access-Control-Allow-Origin: *`，注释写「只被本机扩展调用，允许任意来源没关系」。
        实际不是：绑定 127.0.0.1 只挡了外网，挡不住**用户自己浏览器里打开的任意网页** ——
        那边一个 fetch 就能读走作者库（昵称/抖音号/作品数/状态/标签），还能 POST /queue/clear
        清空待下载区。现在只回显两类来源：
          · `chrome-extension://…`     —— 扩展的 service worker；
          · `http://127.0.0.1:8723` 等 —— 管理后台页面（app.js 自己也调 /queue）。
        其它来源既不回 Allow-Origin，也在 _client_ok() 里直接 403。curl 不带 Origin，照常可用。
        """
        origin = (self.headers.get("Origin") or "").strip()
        low = origin.lower()
        if low.startswith(EXT_ORIGIN_PREFIX) or low in _admin_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-store")

    def _allowed_hosts(self):
        port = int(CFG.get("port") or 8790)
        return {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}

    def _client_ok(self):
        """挡跨站请求与 DNS rebinding（和 8723 的 _client_ok 同一套口径）。"""
        host = (self.headers.get("Host") or "").strip().lower()
        if host and host not in self._allowed_hosts():
            self._json({"ok": False, "error": "forbidden host"}, 403)
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and not origin.startswith(EXT_ORIGIN_PREFIX) and origin not in _admin_origins():
            log("拒绝跨站请求：Origin=%s Host=%s" % (origin, host))
            self._json({"ok": False,
                        "error": "forbidden origin：只允许扩展与管理后台访问本服务"}, 403)
            return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self, limit=200_000):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        if n > limit:
            # 不能只是 return {}：body 没读、连接又会被复用（HTTP/1.1），
            # 残留的 body 会被当成下一个请求行解析 → 后续响应全部错位。
            # 这里直接要求关闭连接，让 socketserver 丢掉这条 socket。
            self.close_connection = True
            log("请求体过大（%d 字节 > %d），已断开连接" % (n, limit))
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        if not self._client_ok():
            return
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        if not self._client_ok():
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        def g(k, d=""):
            return (q.get(k) or [d])[0]

        if path in ("/", "/ping"):
            return self._json({"ok": True, "service": "dy-author-local-bridge",
                               "version": VERSION, "hint": "GET /health /lookup /refresh"})
        if path == "/health":
            s = IDX.summary()
            return self._json({"ok": True, "version": VERSION, **s,
                               "download_db": CFG["download_db"],
                               "db_ok": Path(CFG["download_db"]).exists(),
                               "uptime_s": int(time.time() - START_TS)})
        if path == "/lookup":
            return self._json({"ok": True, **self._do_lookup(
                {"sec_uid": g("sec_uid"), "uid": g("uid"),
                 "nickname": g("nickname"), "aweme_id": g("aweme_id")}, None)})
        if path == "/queue":
            try:
                QUEUE.backfill(IDX)
            except Exception as e:  # noqa: BLE001
                log("待下载区补全失败：", e)
            snap = QUEUE.snapshot()
            # 每条带上本地作品数：前端拿它决定「删除时能不能连主页一起删」
            for it in snap["items"]:
                uid = str(it.get("uid") or "").strip()
                rec = IDX.authors.get(uid) if uid else None
                it["works"] = int((rec or {}).get("file_count") or 0)
            return self._json({"ok": True, **snap})
        if path == "/refresh":
            s = IDX.rebuild()
            return self._json({"ok": True, **s})
        return self._json({"ok": False, "error": "unknown path"}, 404)

    def _do_lookup(self, q: dict, meta: dict | None) -> dict:
        t0 = time.perf_counter()
        res = IDX.lookup(sec_uid=(q.get("sec_uid") or ""), uid=(q.get("uid") or ""),
                         nickname=(q.get("nickname") or ""), aweme_id=(q.get("aweme_id") or ""),
                         meta=meta)
        res["ms"] = round((time.perf_counter() - t0) * 1000, 2)
        res["meta"] = {"indexed_at": int(IDX.indexed_at),
                       "scanned_at": IDX.scanned_at,
                       "authors": len(IDX.authors),
                       "files": IDX.total_files}
        return res

    def _queue_add(self, body: dict) -> dict:
        uid = str(body.get("uid") or "").strip()
        sec = str(body.get("sec_uid") or "").strip()
        if not uid and not sec:
            return {"ok": False, "error": "缺少 uid / sec_uid"}
        rec = IDX.authors.get(uid) or {}
        in_lib = bool(rec.get("file_count")) or bool(IDX.by_sec.get(sec))
        raw_tags = body.get("tags") or rec.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags]        # 传成 "卖号,公司号" 时不要按字符拆
        elif not isinstance(raw_tags, (list, tuple)):
            raw_tags = []
        item = {
            "uid": uid or (IDX.by_sec.get(sec) or ""),
            "sec_uid": sec or (rec.get("sec_uid") or ""),
            "nickname": str(body.get("nickname") or rec.get("nickname") or "")[:60],
            "douyin_id": str(body.get("douyin_id") or rec.get("douyin_id") or "")[:40],
            "follower": _to_int(body.get("follower")),
            "level": str(body.get("level") or rec.get("level") or "")[:12],
            "status": str(body.get("status") or rec.get("status") or "")[:20],
            "tags": [str(t).strip()[:20] for t in raw_tags if str(t).strip()][:8],
            "signature": str(body.get("signature") or "")[:120],
            "avatar": str(body.get("avatar") or "")[:400],
            "in_lib": bool(in_lib),
            "created": False,
            "note": "",
        }
        if not in_lib and item["sec_uid"]:
            # 库里没有这个人：让管理后台按 sec_uid 建档（查 uid、拉资料、写 user_data）
            # 建档失败不能连累入队，任何异常都只写进 note
            try:
                r = admin_create_author(item["sec_uid"])
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "error": "建档异常：%s" % e}
            if r.get("ok"):
                item["created"] = True
                item["note"] = "已新建作者主页"
                # 建档成功才拿到 uid / 昵称 / 抖音号，回填进去：
                # ① 下次推送才能按 uid 判重 ② 列表里不会只显示一串 uid
                if not item.get("uid") and r.get("uid"):
                    item["uid"] = str(r["uid"])
                if not item.get("nickname") and r.get("nickname"):
                    item["nickname"] = str(r["nickname"])[:60]
                if not item.get("douyin_id") and r.get("douyin_id"):
                    item["douyin_id"] = str(r["douyin_id"])
                if not item.get("signature") and r.get("signature"):
                    item["signature"] = str(r["signature"])[:120]
                threading.Thread(
                    target=lambda: (time.sleep(1.5), IDX.rebuild(), QUEUE.backfill(IDX)),
                    daemon=True).start()
            else:
                item["note"] = str(r.get("error") or "建档失败")[:140]
        saved, is_new = QUEUE.add(item)
        return {"ok": True, "added": is_new, "duplicated": not is_new,
                "item": saved, **QUEUE.snapshot()}

    def do_POST(self):
        if not self._client_ok():
            return
        try:
            self._handle_post()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError):
            pass
        except Exception as e:      # noqa: BLE001
            # 以前这里没有任何兜底：body 里一个 "follower": "abc" 就能让 int() 抛 ValueError，
            # 连接被直接关掉，调用方拿不到任何 JSON（2026-11-06 修）
            log("POST %s 处理失败：%s" % (self.path, e))
            try:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    def _handle_post(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if path == "/lookup":
            body = self._read_json()
            return self._json({"ok": True, **self._do_lookup(
                body.get("query") or {}, body.get("meta") or None)})
        if path == "/queue":
            return self._json({"ok": True, **QUEUE.snapshot()})
        if path == "/queue/clear":
            return self._json({"ok": True, **QUEUE.clear()})
        if path == "/queue/undo":
            return self._json({"ok": True, **QUEUE.undo_clear()})
        if path == "/queue/remove":
            body = self._read_json()
            return self._json(QUEUE.remove(uid=body.get("uid") or "",
                                           sec_uid=body.get("sec_uid") or "",
                                           nickname=body.get("nickname") or ""))
        if path == "/queue/add":
            return self._json(self._queue_add(self._read_json()))
        if path == "/refresh":
            s = IDX.rebuild()
            try:
                s["queue_fixed"] = QUEUE.backfill(IDX)
            except Exception as e:  # noqa: BLE001
                log("待下载区补全失败：", e)
            return self._json({"ok": True, **s})
        return self._json({"ok": False, "error": "unknown path"}, 404)


START_TS = time.time()


def _mtimes() -> tuple:
    out = []
    watch = str(Path(CFG["user_data"]).parent / "watch.json")
    for p in (CFG["download_db"], CFG["user_data"], watch):
        try:
            out.append(os.path.getmtime(p))
        except OSError:
            out.append(0)
    return tuple(out)


def auto_reloader():
    """下载记录一变（主程序扫描完新作品），就悄悄重建索引，页面下次查询就是新的。"""
    last = _mtimes()
    while True:
        time.sleep(60)
        try:
            cur = _mtimes()
            if cur != last:
                last = cur
                log("检测到数据文件变化，重建索引…")
                IDX.rebuild()
        except Exception as e:
            log("自动重载失败：", e)


class QuietServer(ThreadingHTTPServer):
    """浏览器扩展偶尔会提前断开连接，这属于正常现象，不要把 traceback 刷满窗口。"""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def main():
    # 用 pythonw（静默模式）启动时 stdout 是 None，直接 print 会崩；改成写日志文件
    try:
        if sys.stdout is None:
            sys.stdout = open(BASE / "服务日志.log", "a", encoding="utf-8", buffering=1)
            sys.stderr = sys.stdout
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = sys.stdout
    print("=" * 62)
    print(f" 抖音作者标记 · 本地查询服务 v{VERSION}")
    print("=" * 62)
    print(f" 数据库：{CFG['download_db']}")
    print(f" 作者库：{CFG['user_data']}")
    print(f" 监听  ：http://{CFG['bind']}:{CFG['port']}  （仅本机）")
    print("-" * 62)
    IDX.rebuild()
    try:
        QUEUE.backfill(IDX)
    except Exception as e:  # noqa: BLE001
        log("待下载区补全失败：", e)
    if CFG.get("auto_reload", True):
        threading.Thread(target=auto_reloader, daemon=True).start()
    try:
        httpd = QuietServer((CFG["bind"], int(CFG["port"])), Handler)
    except OSError as e:
        print(f" 端口 {CFG['port']} 无法绑定，可能服务已经在运行了。{e}")
        print(" （静默模式下这条只写进 服务日志.log）")
        time.sleep(1)
        return
    httpd.daemon_threads = True
    print(f" 服务已就绪。保持本窗口开着，扩展才能查到数据。")
    print(f" 关闭窗口 = 停止服务（扩展会显示「服务未连接」）。")
    print("-" * 62)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
