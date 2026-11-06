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

import json
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


def merge_folder(child_folder, main_folder, dup_ok=True):
    """把 child 文件夹的记录并进 main（同 uid 多文件夹归一用，见 folder_merge.py）。

    和 rename_folder 的区别只有一点：主文件夹里**可能已经有同名文件**
    （两个目录各存了一份），而 `file.rel` 是主键，直接 UPDATE 会撞键。
    dup_ok=True 时先把这些会撞的行删掉（磁盘上那份重复副本马上会被 rmdir 掉，
    留着记录反而会让「文件数」虚高）；dup_ok=False 时什么都不做，交给人工。

    返回 (删除的重复行数, 改前缀的行数)。
    """
    if not child_folder or not main_folder or child_folder == main_folder:
        return 0, 0
    pre = child_folder + "/"
    off = len(pre) + 1
    with _LOCK:
        c = _connect()
        try:
            with c:
                gone = 0
                if dup_ok:
                    cur = c.execute(
                        "DELETE FROM file WHERE folder=?1 AND "
                        "(?2 || '/' || substr(rel, ?3)) IN "
                        "(SELECT rel FROM file WHERE folder=?2)",
                        (child_folder, main_folder, off))
                    gone = cur.rowcount
                cur = c.execute(
                    "UPDATE file SET folder=(:main), rel=(:main || '/' || substr(rel, :off)) "
                    "WHERE substr(rel, 1, :plen) = :pre",
                    {"main": main_folder, "off": off, "plen": len(pre), "pre": pre})
                n = cur.rowcount
                c.execute("UPDATE author SET folder=? WHERE folder=?",
                          (main_folder, child_folder))
            return gone, n
        finally:
            c.close()


def rename_folder(old_folder, new_folder):
    """作者文件夹改名后，同步库里的 rel / folder 两列（前缀替换）。

    2026-11-03 补：以前改名只改磁盘 + data.json，不碰 download_log.db。
    结果 watcher 扫到新文件夹时又 INSERT 一批，老路径的记录没人删（_delete_missing
    是按 folder 精确匹配删的，改名后它查不到老 folder），同一位作者的文件数直接翻倍。
    必须在 os.rename 之后、watcher 反应过来之前调用，此时库里只有旧路径，UPDATE 不会撞主键。
    """
    if not old_folder or not new_folder or old_folder == new_folder:
        return 0
    pre = old_folder + "/"
    off = len(pre) + 1          # substr 从 1 开始计数：跳过 "folder/" 这一段
    with _LOCK:
        c = _connect()
        try:
            with c:
                cur = c.execute(
                    "UPDATE file SET folder=(:new), rel=(:new || '/' || substr(rel, :off)) "
                    "WHERE substr(rel, 1, :plen) = :pre",
                    {"new": new_folder, "off": off, "plen": len(pre), "pre": pre})
                n = cur.rowcount
                c.execute("UPDATE author SET folder=? WHERE folder=?", (new_folder, old_folder))
            return n
        finally:
            c.close()


def recount_author(uid):
    """按 file 表现算某个作者的 file_count / bytes / last_download（清理残留记录后用）。"""
    uid = str(uid or "")
    if not uid:
        return None
    with _LOCK:
        c = _connect()
        try:
            with c:
                cnt, byt, last = c.execute(
                    "SELECT COUNT(*), COALESCE(SUM(size),0), COALESCE(MAX(mtime),0) "
                    "FROM file WHERE uid=?", (uid,)).fetchone()
                c.execute("UPDATE author SET file_count=?, bytes=?, last_download=? WHERE uid=?",
                          (cnt, byt, last, uid))
            return {"files": cnt, "bytes": byt, "last_download": last}
        finally:
            c.close()


def drop_folder(folder):
    """整段删掉某个文件夹名下的全部文件记录（改名后残留的旧路径用这个清）。"""
    folder = (folder or "").strip()
    if not folder:
        return 0
    with _LOCK:
        c = _connect()
        try:
            with c:
                cur = c.execute("DELETE FROM file WHERE folder=?", (folder,))
                n = cur.rowcount
                c.execute("DELETE FROM author WHERE folder=?", (folder,))
            return n
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


def recent(since=0, limit=600, uid=None, cursor=None, uids=None):
    """最近下载的文件（按 mtime 倒序），给「新下载」页用。

    mtime 是文件落盘时间 ≈ 下载完成时间，所以「今天下载的」= mtime >= 今天 0 点。
    返回 (符合条件的总文件数, 本页行, 下一页 cursor, 还有没有下一页)。

    uids: 作者属性筛选算出来的白名单（「新下载」页那套和「导出作者信息」一致的筛选）。
    用 json_each 传数组而不是拼一堆 ? ——作者上千个时占位符会撞 SQLite 的变量上限。

    2026-11-02：不再有「只看最新 N 个」的硬上限，改成一页一页往下翻。
    分页用 keyset（cursor = "mtime|rel"，rel 是主键保证唯一），不用 OFFSET：
    翻页途中又有新文件落盘时，OFFSET 会整体错位（重复或漏掉），
    而 (mtime, rel) 是排好序的稳定坐标，多取一条还能判断「还有没有下一页」。
    只读，不改库。
    """
    try:
        c = _connect()
    except sqlite3.Error:
        return 0, [], None, False
    limit = max(1, int(limit))
    try:
        base_where = "mtime >= ?"
        base_args = [int(since or 0)]
        if uid:
            base_where += " AND uid = ?"
            base_args.append(uid)
        elif uids is not None:
            uids = list(uids)
            if not uids:
                return 0, [], None, False
            base_where += " AND uid IN (SELECT value FROM json_each(?))"
            base_args.append(json.dumps([str(u) for u in uids], ensure_ascii=False))
        total = c.execute("SELECT COUNT(*) FROM file WHERE " + base_where, base_args).fetchone()[0]

        where, args = base_where, list(base_args)
        if cursor:
            try:
                cm, crel = str(cursor).split("|", 1)
                cm = int(cm)
            except (TypeError, ValueError):
                cm, crel = None, None
            if cm is not None:
                where += " AND (mtime < ? OR (mtime = ? AND rel < ?))"
                args += [cm, cm, crel]
        rows = c.execute(
            "SELECT rel, uid, name, size, mtime FROM file WHERE " + where +
            " ORDER BY mtime DESC, rel DESC LIMIT ?", args + [limit + 1]).fetchall()
    except sqlite3.Error:
        return 0, [], None, False
    finally:
        c.close()
    more = len(rows) > limit
    rows = rows[:limit]
    nxt = ("%d|%s" % (rows[-1][4] or 0, rows[-1][0])) if rows else None
    return total or 0, [{"rel": r[0], "uid": r[1], "name": r[2] or "",
                         "size": r[3] or 0, "mtime": r[4] or 0} for r in rows], nxt, more


if __name__ == "__main__":
    t = totals()
    print("下载记录库 %s" % DB_PATH)
    print("作者 %d 位 / 文件 %d 个 / %.1f GB" % (t["authors"], t["files"], t["bytes"] / 1024 / 1024 / 1024))
