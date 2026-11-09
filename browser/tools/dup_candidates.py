#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""找出「同一作者 + 同一天 + 描述规范化后相同」但文件名不同的作品组。

只做候选分组，不做删除判断 —— 是否真的重复必须靠画面比对（见 compare_frames.py）。
判定口径沿用 2026-11-09 那轮核对：
  - 文件名 = `YYYY-MM-DD-昵称-描述[.ext]`，第 0 段日期，第 1 段昵称
  - 描述规范化 = 去空白 / 去下划线 / 去表情（deco_strip）
  - 组内若文件名里带不同 aweme_id 的（追更文件名不含 ID，DouK 同名会带），
    仍列出，但标记 has_ids
"""
import os
import re
import sys
import json
import collections

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from paths import SCAN_ROOT                      # noqa: E402
from watch_core import deco_strip                # noqa: E402

DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".flv", ".mkv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}


def split_name(name):
    """返回 (日期, 描述) —— 昵称段跳过，其余当描述。"""
    stem, ext = os.path.splitext(name)
    m = DATE_RE.match(stem)
    date = m.group(1) if m else ""
    rest = stem[len(date):].lstrip("-_ ")
    parts = rest.split("-", 1)
    desc = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    # 图文第 2、3 张：`..._2`
    desc = re.sub(r"_\d+$", "", desc)
    return date, desc, ext.lower()


def main():
    groups = collections.defaultdict(list)
    total = 0
    for d in os.listdir(SCAN_ROOT):
        adir = os.path.join(SCAN_ROOT, d)
        if not os.path.isdir(adir):
            continue
        for root, _dirs, files in os.walk(adir):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext not in VIDEO_EXT and ext not in IMAGE_EXT:
                    continue
                total += 1
                date, desc, ext2 = split_name(f)
                if not date:
                    continue
                key = deco_strip(desc)
                if len(key) < 2:
                    continue
                groups[(d, date, key)].append(os.path.join(root, f))

    out = []
    for (author, date, key), paths in groups.items():
        if len(paths) < 2:
            continue
        out.append({
            "author": author,
            "date": date,
            "key": key,
            "files": sorted(paths),
            "sizes": [os.path.getsize(p) for p in sorted(paths)],
        })
    out.sort(key=lambda x: (x["author"], x["date"]))
    print("扫描文件 %d，候选组 %d" % (total, len(out)))
    json.dump(out, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "dup_candidates.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    for g in out:
        print("\n%s | %s | %s" % (g["author"], g["date"], g["key"]))
        for p, s in zip(g["files"], g["sizes"]):
            print("   %9.1fKB  %s" % (s / 1024, os.path.basename(p)))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
