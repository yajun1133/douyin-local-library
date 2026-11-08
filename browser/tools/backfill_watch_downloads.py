#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 data/watch.log 回填「追更下载明细」（data/watch_downloads.jsonl）。

为什么能回填：watch.log 里每条 `↓ 11-08 20:53  描述 (1.7 MB)` 前面的方括号时间，
**就是那个文件落盘的秒**（实测与 download_log.db 的 file.mtime 完全相等）。
所以按「日志时间 ±3 秒」去 file 表里捞，捞到的就是这个作品的全部文件（图文会好几张）。

用法：
  python tools/backfill_watch_downloads.py            # 回填（幂等：已有 rel 会跳过）
  python tools/backfill_watch_downloads.py --dry      # 只看能捞到多少，不写文件
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, HERE)

import paths          # noqa: E402
import scanner as _sc  # noqa: E402
import watch_core as W  # noqa: E402

LOG = W.LOG_FILE
DB = os.path.join(paths.DATA_DIR, "download_log.db")
LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+↓\s")


def base_of(name):
    """作品基准名：去掉扩展名和图文第 N 张的 `_1.._9` 后缀。

    同一件图文作品的 6 张图会是 `…_1.jpeg` … `…_6.jpeg`，基准名一致。
    """
    return re.sub(r"_\d+$", "", os.path.splitext(name or "")[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只统计，不写文件")
    args = ap.parse_args()

    if not os.path.isfile(LOG):
        print("找不到 %s" % LOG)
        return 1
    if not os.path.isfile(DB):
        print("找不到 %s" % DB)
        return 1

    stamps = []
    for ln in open(LOG, "r", encoding="utf-8", errors="replace"):
        m = LINE.match(ln)
        if m:
            try:
                stamps.append(int(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()))
            except ValueError:
                pass
    stamps = sorted(set(stamps))
    print("日志里 ↓ 记录：%d 条（去重后）" % len(stamps))

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    # 第一步：只认「落盘时间精确等于日志时间」的文件。
    # 不能用 ±N 秒的窗口 —— 实测 ±1 秒就会把相邻那条作品的图一起捞进来（文件数从 291 跳到 509）。
    # 精确匹配拿到的是每件作品的第一张/唯一一张，够用来定位「是哪件作品」。
    hits = {}       # (uid, base) -> mtime
    miss = []
    for ts in stamps:
        rows = conn.execute(
            "select rel, uid, name, mtime from file where mtime = ?", (ts,)).fetchall()
        if not rows:
            miss.append(ts)
            continue
        for f in rows:
            hits[(str(f["uid"] or ""), base_of(f["name"]))] = int(f["mtime"] or ts)
    print("精确定位到的作品：%d 件" % len(hits))
    print("对不上文件的时间点：%d 个（文件多半已被删除/移走）" % len(miss))

    # 第二步：图文作品的多张图 mtime 会差一两秒，靠「同作者 + 同基准名」把它们补齐。
    # 基准名里带日期和描述，撞名的概率可以忽略；仍按基准名精确比对，不做模糊。
    by_uid = {}
    for uid, base in hits:
        by_uid.setdefault(uid, set()).add(base)
    files_of = {}
    for uid, bases in by_uid.items():
        for f in conn.execute("select rel, name, size from file where uid = ?", (uid,)):
            b = base_of(f["name"])
            if b in bases:
                files_of.setdefault((uid, b), []).append((f["rel"], int(f["size"] or 0)))

    done = set()
    for r in W.load_downloads():
        done.update(r.get("rels") or [])

    works = []
    for (uid, base), ts in hits.items():
        fl = files_of.get((uid, base)) or []
        rels = [r for r, _ in fl if r not in done]
        if not rels:
            continue
        info = _sc.parse_work_filename(os.path.basename(rels[0])) or {}
        works.append({
            "ts": ts,
            "uid": uid,
            "nickname": (info.get("author") or "").strip(),
            "date": (info.get("date") or "").strip()
                    or datetime.fromtimestamp(ts).strftime("%Y-%m-%d"),
            "desc": (info.get("desc") or "").strip(),
            "rels": rels,
            "size": sum(s for r, s in fl if r in rels),
            "backfill": True,
        })
    works.sort(key=lambda r: r["ts"])
    print("可回填：%d 件作品 / %d 个文件" % (len(works), sum(len(r["rels"]) for r in works)))

    if args.dry:
        for r in works[-5:]:
            print("  %s  %-12s %s  %d 个文件" % (
                datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M:%S"),
                r["nickname"][:12], (r["desc"] or "")[:24], len(r["rels"])))
        print("（--dry，没有写文件）")
        return 0

    n = 0
    for r in works:
        if W.record_download(r["uid"], r["nickname"], r["date"], r["desc"],
                             r["rels"], r["size"], ts=r["ts"], backfill=True):
            n += 1
    print("已写入 %d 条 → %s" % (n, W.DOWNLOADS_FILE))
    print("明细总条数：%d" % W.download_count())
    return 0


if __name__ == "__main__":
    sys.exit(main())
