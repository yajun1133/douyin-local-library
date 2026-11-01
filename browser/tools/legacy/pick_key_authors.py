#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从收藏博主汇总 CSV 里筛出"重点博主"（被收藏 N 条以上），生成精简清单。"""
import csv, glob, os, sys
from datetime import datetime

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
import sys  # noqa: E402
sys.path.insert(0, _TOOLS)                       # tools/ 下的 blacklist、downloaded
sys.path.insert(0, os.path.join(_ROOT, "src"))   # src/paths.py 统一路径
import paths  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # 仅用于拼本目录内的东西
OUT_DIR = paths.EXPORT_DIR
MIN = int(sys.argv[1]) if len(sys.argv) > 1 else 2

src = max(glob.glob(os.path.join(OUT_DIR, "收藏博主汇总_*.csv")), key=os.path.getmtime)
rows = list(csv.DictReader(open(src, encoding="utf-8-sig")))
rows = [r for r in rows if int(r["count"]) >= MIN]
rows.sort(key=lambda r: -int(r["count"]))

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
f_csv = os.path.join(OUT_DIR, f"重点博主_收藏{MIN}条以上_{stamp}.csv")
f_txt = os.path.join(OUT_DIR, f"重点博主_未下载_链接_{MIN}条_{stamp}.txt")
f_all_txt = os.path.join(OUT_DIR, f"重点博主_全部_链接_{MIN}条_{stamp}.txt")

cols = ["sec_uid", "uid", "nickname", "douyin_id", "count", "last_work",
        "downloaded", "known_as", "author_url"]
with open(f_csv, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader(); w.writerows(rows)

pend = [r for r in rows if r["downloaded"] == "未下载" and r["sec_uid"]]
with open(f_txt, "w", encoding="utf-8") as f:
    f.writelines(r["author_url"] + "\n" for r in pend)
with open(f_all_txt, "w", encoding="utf-8") as f:
    f.writelines(r["author_url"] + "\n" for r in rows if r["sec_uid"])

print(f"收藏>={MIN}条 的博主：{len(rows)} 个（未下载 {len(pend)} 个）")
print(f"[表]   {f_csv}")
print(f"[链接·仅未下载] {f_txt}")
print(f"[链接·全部]     {f_all_txt}")
