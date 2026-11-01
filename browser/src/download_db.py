# -*- coding: utf-8 -*-
"""下载记录库：我们自己记的「谁什么时候下过、下了多少」。

为什么不用 DouK 自带的下载记录：它的 `download_data` 表只有一个字段 `ID`（作品 aweme_id，
现在 6.7 万条），**既没有作者、也没有时间**，所以「这个作者多久没下载过」在它那儿根本查不出来
（`mapping_data` 也只有 ID + 标题）。但**文件的写入时间就是下载完成时间**，扫描时顺手把每个
文件的 mtime 记下来，就能按作者算出「最后一次下载是什么时候」。

所以这个库是扫描器的副产物，不额外读盘：
  file    每个作品文件一行（rel 唯一）：size / mtime / 第一次看到 / 最近看到
  author  每个作者一行：文件数 / 总字节 / 最后下载时间（max(mtime)）/ 最近扫描时间

放在 `data/download_log.db`（SQLite，WAL 模式：扫描器写的同时网页能读）。
"""

import os
import sqlite3
import threading
import time

import paths

DB_PATH = os.path.join(paths.DATA_DIR, "download_log.db")

_LOCK = threading.Lock()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS file (
    rel        TEXT PRIMARY KEY,
    uid        TEXT NOT NULL,
    folder     TEXT NOT NULL,
    name       TEXT NOT NULL,
    size       INTEGER DEFAULT 0,
    mtime      INTEGER DEFAULT 0,   -- 文件写入时间（≈ 下载完成时间）
    first_seen TEXT,                -- 本程序第一次看到这条文件的时间
    last_seen  TEXT
);
CREATE INDEX IF NOT EXISTS idx_file_uid ON file(uid);
CREATE INDEX IF NOT EXISTS idx_file_folder ON file(folder);

CREATE TABLE IF NOT EXISTS author (
    uid          TEXT PRIMARY KEY,
    folder       TEXT,
    file_count   INTEGER DEFAULT 0,
    bytes        INTEGER DEFAULT 0,
    last_download INTEGER DEFAULT 0,  -- max(mtime)：这个作者最后一次下载是什么时候
    last_scan    TEXT
);
"""


def _connect():
    """每次开连接（扫描器和网页是两个进程/线程，长连接容易踩锁）。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(_SCHEMA)
    return c


def _delete_missing(c, folder, rels):
    """这个文件夹里已经不存在的文件要从库里删掉（删作品/改名都会走到）。"""
    keep = set(rels)
    rows = c.execute("SELECT rel FROM file WHERE folder=?", (folder,)).fetchall()
    gone = [r[0] for r in rows if r[0] not in keep]
    for i in range(0, len(gone), 400):
        chunk = gone[i:i + 400]
        c.execute("DELETE FROM file WHERE rel IN (%s)" % ",".join("?" * len(chunk)), chunk)
    return len(gone)


def record_folder(uid, folder, files, now=None):
    """记下一个作者文件夹当前的全部文件（只在文件夹 mtime 变了时才调用）。

    files: [{"rel","name","size","mtime"}, ...]
    新增的文件 first_seen = 现在；老文件保留原来的 first_seen，只更新 size/mtime/last_seen。
    """
    if not uid or not folder:
        return None
    now = now or time.strftime("%Y-%m-%d %H:%M:%S")
    with _LOCK:
        c = _connect()
        try:
            with c:
                for f in files:
                    c.execute(
                        "INSERT INTO file(rel, uid, folder, name, size, mtime, first_seen, last_seen) "
                        "VALUES(?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(rel) DO UPDATE SET "
                        "  uid=excluded.uid, folder=excluded.folder, name=excluded.name, "
                        "  size=excluded.size, mtime=excluded.mtime, last_seen=excluded.last_seen",
                        (f.get("rel"), uid, folder, f.get("name") or os.path.basename(f.get("rel") or ""),
                         int(f.get("size") or 0), int(f.get("mtime") or 0), now, now))
                gone = _delete_missing(c, folder, [f.get("rel") for f in files])
                cnt, byt, last = c.execute(
                    "SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(MAX(mtime),0) "
                    "FROM file WHERE uid=?", (uid,)).fetchone()
                c.execute(
                    "INSERT INTO author(uid, folder, file_count, bytes, last_download, last_scan) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET "
                    "  folder=excluded.folder, file_count=excluded.file_count, "
                    "  bytes=excluded.bytes, last_download=excluded.last_download, "
                    "  last_scan=excluded.last_scan",
                    (uid, folder, cnt, byt, last, now))
            return {"files": len(files), "removed": gone, "last_download": last}
        finally:
            c.close()


def summary():
    """{uid: {"count","last","bytes","scan"}} —— 服务启动时一次拿全（1700 行很快）。"""
    out = {}
    try:
        c = _connect()
    except sqlite3.Error:
        return out
    try:
        for uid, cnt, last, byt, scan in c.execute(
                "SELECT uid, file_count, last_download, bytes, last_scan FROM author"):
            out[uid] = {"count": cnt or 0, "last": last or 0, "bytes": byt or 0, "scan": scan or ""}
    except sqlite3.Error:
        pass
    finally:
        c.close()
    return out


def author_info(uid):
    try:
        c = _connect()
    except sqlite3.Error:
        return {}
    try:
        row = c.execute("SELECT file_count, bytes, last_download, last_scan FROM author WHERE uid=?",
                        (uid,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        c.close()
    if not row:
        return {}
    return {"count": row[0] or 0, "bytes": row[1] or 0, "last": row[2] or 0, "scan": row[3] or ""}


def totals():
    try:
        c = _connect()
    except sqlite3.Error:
        return {"authors": 0, "files": 0, "bytes": 0}
    try:
        a = c.execute("SELECT COUNT(*) FROM author").fetchone()[0]
        f, b = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM file").fetchone()
    except sqlite3.Error:
        a, f, b = 0, 0, 0
    finally:
        c.close()
    return {"authors": a or 0, "files": f or 0, "bytes": b or 0}


def recent(since=0, limit=600, uid=None):
    """最近下载的文件（按 mtime 倒序），给「新下载」页用。

    mtime 是文件落盘时间 ≈ 下载完成时间，所以「今天下载的」= mtime >= 今天 0 点。
    返回 (符合条件的总文件数, [{"rel","uid","name","size","mtime"}, ...])。
    只读，不改库；limit 只截返回行数，总数照实算。
    """
    try:
        c = _connect()
    except sqlite3.Error:
        return 0, []
    try:
        where = "mtime >= ?"
        args = [int(since or 0)]
        if uid:
            where += " AND uid = ?"
            args.append(uid)
        total = c.execute("SELECT COUNT(*) FROM file WHERE " + where, args).fetchone()[0]
        rows = c.execute(
            "SELECT rel, uid, name, size, mtime FROM file WHERE " + where +
            " ORDER BY mtime DESC LIMIT ?", args + [max(1, int(limit))]).fetchall()
    except sqlite3.Error:
        return 0, []
    finally:
        c.close()
    return total or 0, [{"rel": r[0], "uid": r[1], "name": r[2] or "",
                         "size": r[3] or 0, "mtime": r[4] or 0} for r in rows]


if __name__ == "__main__":
    t = totals()
    print("下载记录库 %s" % DB_PATH)
    print("作者 %d 位 / 文件 %d 个 / %.1f GB" % (t["authors"], t["files"], t["bytes"] / 1024 / 1024 / 1024))
