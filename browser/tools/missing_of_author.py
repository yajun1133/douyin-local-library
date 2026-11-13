# -*- coding: utf-8 -*-
"""对一位作者做「本地缺哪条」核对：默认只列清单，加 --apply 才下。

用法：
    C:/Python312/python.exe tools/missing_of_author.py <uid> [--apply]

判重口径与追更 scan_round 完全一致（四层：seen / 基线之外的 ID 账本 / 文件名 / DouK 记录），
所以「它说缺」= 追更也认为该下。追更常驻在跑也可以直接用，只是会多打几枪接口。
"""
import os
import sys
import json
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
import paths          # noqa: E402
import watch_core as W  # noqa: E402

UID = sys.argv[1]
APPLY = "--apply" in sys.argv

watch = json.load(open(W.WATCH_FILE, encoding="utf-8"))
item = next(a for a in watch["authors"] if str(a["uid"]) == UID)
sec, nick = item["sec_uid"], item["nickname"]

lib_idx, lib_folders = W.load_library_index()
local = lib_idx.get(UID) or set()
date_idx = W.build_date_index(local)
folder = lib_folders.get(UID) or ""
author_dir = os.path.join(paths.SCAN_ROOT, folder) if folder else ""
douk_ids = W.douk_downloaded_ids()
dl_ids = W.watch_downloaded_ids()
print("作者 %s  本地 %d 条  目录=%s" % (nick, len(local), folder))
print("DouK 记录 %d 条 / 追更明细 ID %d 条" % (len(douk_ids), len(dl_ids)))

dy = W.Douyin()
al = dy.author_works(sec, pages=10)
print("接口返回 %d 条" % len(al))

miss = []
for a in al:
    aid = str(a.get("aweme_id") or "")
    ct = int(a.get("create_time") or 0)
    date = datetime.fromtimestamp(ct).strftime("%Y-%m-%d")
    desc = a.get("desc") or ""
    if aid in douk_ids:
        continue
    if aid in dl_ids:
        continue
    if W.is_known(local, date_idx, date, desc, 64):
        continue
    miss.append((ct, aid, date, desc))

miss.sort()
print("\n本地缺 %d 条：" % len(miss))
for ct, aid, date, desc in miss:
    nm = W.douk_detail_name(date, nick, desc, aid)
    print("  %s %s  | desc=%r  -> %s.mp4" % (date, aid, desc, nm))
    print("     目标已存在？", bool(W.existing_of(os.path.join(author_dir, nm), (W.VIDEO_EXT,)))
          if author_dir else "no-dir")

if APPLY and miss:
    recs = []
    for ct, aid, date, desc in miss:
        aw = next(x for x in al if str(x.get("aweme_id")) == aid)
        got = []
        files, size, err = W.download_aweme(dy, aw, author_dir, nick, 64, out=got)
        print("  ->", aid, files, size, err, got)
        if files:
            rels = [os.path.relpath(p, paths.SCAN_ROOT).replace(os.sep, "/") for p in got]
            W.record_download(UID, nick, date, desc, rels, size, aid=aid)
            recs.append((aid, rels))
    st = W.load_state()
    s = st["authors"].setdefault(UID, {"last_ts": 0, "seen": []})
    for aid, _r in recs:
        if aid not in s["seen"]:
            s["seen"].append(aid)
    W.save_state(st)
    print("已补 %d 条并写状态" % len(recs))
