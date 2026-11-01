#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从已抓取的收藏原始数据（jsonl）生成报表，不再访问网络。

用法：
  python favorites_report.py                          # 用最新的 jsonl 全量生成
  python favorites_report.py --limit 3000             # 只取最近收藏的 3000 条
  python favorites_report.py --file xxx.jsonl         # 指定输入
"""

import argparse
import csv
import glob
import gzip
import json
import os
import sys
from datetime import datetime

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
import sys  # noqa: E402
sys.path.insert(0, _TOOLS)                       # tools/ 下的 blacklist、downloaded
sys.path.insert(0, os.path.join(_ROOT, "src"))   # src/paths.py 统一路径
import paths  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # 仅用于拼本目录内的东西
OUT_DIR = paths.EXPORT_DIR
USER_DATA = paths.USER_DATA
DATA_JSON = paths.DATA_JSON


def parse_aweme(a):
    au = a.get("author") or {}
    stats = a.get("statistics") or {}
    ct = a.get("create_time")
    return {
        "aweme_id": a.get("aweme_id"),
        "desc": (a.get("desc") or "").replace("\n", " ").strip(),
        "create_time": datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M") if ct else "",
        "media_type": "图文" if a.get("images") else "视频",
        "digg_count": stats.get("digg_count", ""),
        "sec_uid": au.get("sec_uid", ""),
        "uid": au.get("uid", ""),
        "nickname": au.get("nickname", ""),
        "douyin_id": au.get("unique_id") or au.get("short_id") or "",
        "aweme_url": f"https://www.douyin.com/video/{a.get('aweme_id')}",
        "author_url": f"https://www.douyin.com/user/{au.get('sec_uid', '')}",
    }


def load_known():
    known_uid, known_sec = {}, {}
    for path in (DATA_JSON, USER_DATA):
        if not os.path.isfile(path):
            continue
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        for uid, v in (d.get("accounts") or {}).items():
            known_uid[str(uid)] = v.get("nickname") or ""
            if v.get("sec_uid"):
                known_sec[v["sec_uid"]] = v.get("nickname") or ""
    tbl = os.path.join(SCRIPT_DIR, "uid_secuid_对照表.csv")
    if os.path.isfile(tbl):
        with open(tbl, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("uid"):
                    known_uid.setdefault(row["uid"], row.get("昵称(权威)", ""))
                if row.get("sec_uid"):
                    known_sec.setdefault(row["sec_uid"], row.get("昵称(权威)", ""))
    # 下载目录（权威依据，data.json 滞后不可靠）
    try:
        import downloaded as DL
        dl = DL.get()
        for rec in dl.records:
            known_uid.setdefault(rec["uid"], rec["folder_nickname"])
        print(f"[已下载] 下载目录识别到 {len(dl)} 个账号")
    except Exception as e:
        print(f"[提示] 读取下载目录失败，仅用旧索引：{e}")
    return known_uid, known_sec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="", help="输入 jsonl")
    ap.add_argument("--limit", type=int, default=0, help="只取最近 N 条收藏（0=全部）")
    args = ap.parse_args()

    cands = []
    for pat in ("收藏原始数据_*.jsonl", "喜欢原始数据_*.jsonl",
                "收藏原始数据_*.jsonl.gz", "喜欢原始数据_*.jsonl.gz"):
        cands += glob.glob(os.path.join(OUT_DIR, pat))
    src = args.file or max(cands, key=os.path.getmtime, default="")
    if not src or not os.path.isfile(src):
        sys.exit("[!] 找不到原始数据 jsonl")

    print(f"[输入] {src} ({os.path.getsize(src) / 1048576:.0f} MB)")
    rows, seen, page = [], set(), 0
    opener = gzip.open if src.endswith(".gz") else open
    with opener(src, "rt", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line.startswith("{"):
                continue
            page += 1
            try:
                d = json.loads(line)
            except Exception:
                continue
            for a in (d.get("aweme_list") or []):
                aid = a.get("aweme_id")
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                rows.append(parse_aweme(a))
                if args.limit and len(rows) >= args.limit:
                    break
            if page % 200 == 0:
                print(f"  已解析 {page} 页 / {len(rows)} 条")
            if args.limit and len(rows) >= args.limit:
                break

    if not rows:
        sys.exit("[!] 没解析到数据")

    # 去重按收藏顺序（接口返回顺序即收藏时间倒序）
    known_uid, known_sec = load_known()
    authors = {}
    for r in rows:
        k = r["sec_uid"] or r["uid"]
        if not k:
            continue
        a = authors.setdefault(k, {
            "sec_uid": r["sec_uid"], "uid": r["uid"], "nickname": r["nickname"],
            "douyin_id": r["douyin_id"], "count": 0, "_latest": "",
            "sample_aweme": r["aweme_id"], "author_url": r["author_url"],
        })
        a["count"] += 1
        if r["create_time"] > a["_latest"]:
            a["_latest"] = r["create_time"]
            a["nickname"] = r["nickname"] or a["nickname"]
        if not a["douyin_id"] and r["douyin_id"]:
            a["douyin_id"] = r["douyin_id"]

    out = []
    # 黑名单剔除（不下载的博主，收藏/喜欢里出现也一律不要）
    sys.path.insert(0, SCRIPT_DIR)
    import blacklist as BL
    blocked = []
    for k in list(authors):
        a = authors[k]
        if BL.get().match(sec_uid=a["sec_uid"], uid=a["uid"], nickname=a["nickname"]):
            blocked.append(authors.pop(k))
    if blocked:
        names = "、".join((b["nickname"] or b["uid"]) for b in blocked[:5])
        print(f"[黑名单] 已剔除 {len(blocked)} 个不下载的博主：{names}{'…' if len(blocked) > 5 else ''}")

    for a in authors.values():
        dl = (a["uid"] in known_uid) or (a["sec_uid"] in known_sec)
        a["downloaded"] = "已下载" if dl else "未下载"
        a["known_as"] = known_uid.get(a["uid"]) or known_sec.get(a["sec_uid"]) or ""
        a["last_work"] = a.pop("_latest")
        out.append(a)
    out.sort(key=lambda x: (-x["count"], x["nickname"]))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = f"_最近{args.limit}条" if args.limit else ""
    f_detail = os.path.join(OUT_DIR, f"收藏视频明细{tag}_{stamp}.csv")
    f_authors = os.path.join(OUT_DIR, f"收藏博主汇总{tag}_{stamp}.csv")
    f_pending = os.path.join(OUT_DIR, f"未下载博主_主页链接{tag}_{stamp}.txt")

    cols = list(rows[0].keys())
    with open(f_detail, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    acols = ["sec_uid", "uid", "nickname", "douyin_id", "count", "last_work",
             "downloaded", "known_as", "author_url", "sample_aweme"]
    with open(f_authors, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=acols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)

    pending = [a for a in out if a["downloaded"] == "未下载" and a["sec_uid"]]
    with open(f_pending, "w", encoding="utf-8") as f:
        for a in pending:
            f.write(a["author_url"] + "\n")

    print("\n" + "=" * 56)
    print(f"收藏视频：{len(rows)} 条")
    print(f"涉及博主：{len(out)} 个（已下载 {len(out) - len(pending)} / 未下载 {len(pending)}）")
    print("=" * 56)
    print(f"[明细] {f_detail}")
    print(f"[汇总] {f_authors}")
    print(f"[待下载] {f_pending}")


if __name__ == "__main__":
    main()
