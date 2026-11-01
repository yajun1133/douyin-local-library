#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把网页导出的「作者信息_*.csv」转成 DouK 下载器的账号列表 JSON，
并可直接写进 DouK 的 Volume/settings.json（accounts_urls）。

CSV 列（导出功能给的固定表头）：
    昵称, 别名/备注, 抖音号, UID, SEC_UID, 等级, 标签, 状态, 主页链接
只要 SEC_UID 或 主页链接 有一个就能用；mark 取「昵称」，enable 一律 true。

用法：
    python tools/csv_to_douk.py                          # 自动取 export/ 下最新的作者信息 csv，只生成 JSON
    python tools/csv_to_douk.py --csv 某文件.csv          # 指定 csv
    python tools/csv_to_douk.py --write --merge          # 写进 settings.json：保留原有条目，追加缺的
    python tools/csv_to_douk.py --write --replace        # 写进 settings.json：整个 accounts_urls 换成这份
    python tools/csv_to_douk.py --write --replace --dry  # 只看会改成什么，不动文件

写 settings.json 前会自动备份到 backup/douk_settings_<时间>.json。
DouK 原文件是 UTF-8 BOM + CRLF + 4 空格缩进，这里照原样写回，避免它自己再解析一遍出幺蛾子。
"""

import argparse
import csv
import glob
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import paths  # noqa: E402

SETTINGS = paths.DOWNLOADER_SETTINGS


# ---------------- 读 CSV ----------------
def latest_csv():
    files = sorted(glob.glob(os.path.join(paths.EXPORT_DIR, "作者信息_*.csv")))
    if not files:
        raise SystemExit("export/ 下没有 作者信息_*.csv，用 --csv 指定一份")
    return files[-1]


def read_authors(path):
    """返回 [{mark, sec_uid, uid, nickname, url}]，按 SEC_UID 去重，保持 CSV 顺序。"""
    out, seen = [], set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sec = (row.get("SEC_UID") or "").strip()
            url = (row.get("主页链接") or "").strip().rstrip("/")
            if not sec:
                sec = url.split("/")[-1] if "/user/" in url else ""
            if not sec:
                continue                                  # 没有 sec_uid 的下不了，跳过
            if sec in seen:
                continue
            seen.add(sec)
            out.append({
                "mark": (row.get("昵称") or "").strip() or sec,
                "sec_uid": sec,
                "uid": (row.get("UID") or "").strip(),
                "nickname": (row.get("昵称") or "").strip(),
                "url": "https://www.douyin.com/user/" + sec,
            })
    return out


def to_douk(authors):
    """转成 DouK 的条目结构，键顺序与它自己写出来的一致。"""
    return [{
        "mark": a["mark"],
        "url": a["url"],
        "tab": "",
        "earliest": "",
        "latest": "",
        "enable": True,
    } for a in authors]


# ---------------- 读写 settings.json ----------------
def load_settings():
    with open(SETTINGS, encoding="utf-8-sig") as f:
        return json.load(f)


def save_settings(data):
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="\r\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, SETTINGS)


def backup_settings():
    os.makedirs(paths.BACKUP_DIR, exist_ok=True)
    dst = os.path.join(
        paths.BACKUP_DIR, "douk_settings_%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(SETTINGS, dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--write", action="store_true", help="写进 DouK 的 settings.json")
    ap.add_argument("--merge", action="store_true", help="保留原有条目，只追加没有的")
    ap.add_argument("--replace", action="store_true", help="整个 accounts_urls 换成这份")
    ap.add_argument("--dry", action="store_true", help="只打印结果，不落盘")
    a = ap.parse_args()

    src = a.csv or latest_csv()
    authors = read_authors(src)
    if not authors:
        raise SystemExit("CSV 里没解析出任何作者")

    entries = to_douk(authors)

    # 1) 永远先产出一份独立 JSON（可以直接看、也可以手动贴）
    base = os.path.splitext(os.path.basename(src))[0]
    out_json = os.path.join(paths.EXPORT_DIR, "douk_" + base + ".json")
    if not a.dry:
        with open(out_json, "w", encoding="utf-8", newline="\n") as f:
            json.dump(entries, f, ensure_ascii=False, indent=4)
    print("来源 CSV : %s" % src)
    print("作者条数 : %d" % len(authors))
    print("独立 JSON: %s" % out_json)

    if not a.write:
        return

    if a.merge == a.replace:
        raise SystemExit("--write 需要且只能给一个：--merge 或 --replace")

    data = load_settings()
    old = data.get("accounts_urls") or []
    old_sec = {e.get("url", "").rstrip("/").split("/")[-1] for e in old}
    new_sec = {e["url"].rstrip("/").split("/")[-1] for e in entries}

    if a.merge:
        added = [e for e in entries if e["url"].rstrip("/").split("/")[-1] not in old_sec]
        data["accounts_urls"] = old + added
        print("原有 %d 条，新增 %d 条，合并后 %d 条"
              % (len(old), len(added), len(data["accounts_urls"])))
    else:
        keep = [e for e in old if e.get("url", "").rstrip("/").split("/")[-1] not in new_sec]
        data["accounts_urls"] = entries
        print("替换：原有 %d 条 → %d 条" % (len(old), len(entries)))
        if keep:
            print("原有但不在本次 CSV 里的 %d 条已被移除（备份里能找回）：" % len(keep))
            for e in keep:
                print("   · %s" % e.get("mark") or e.get("url"))

    if a.dry:
        print("[dry] 没有真正写入")
        return

    bak = backup_settings()
    save_settings(data)
    print("已备份原配置: %s" % bak)
    print("已写入      : %s" % SETTINGS)


if __name__ == "__main__":
    main()
