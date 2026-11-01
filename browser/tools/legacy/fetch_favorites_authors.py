#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
批量导出抖音「收藏」里所有视频的博主（sec_uid / uid / 昵称）。

原理：
  复用 DouK-Downloader 的登录 Cookie + 内置 ABogus 签名（纯 Python，无需浏览器），
  调用 https://www.douyin.com/aweme/v1/web/aweme/listcollection/ 接口翻页，
  从每条作品的 author 字段直接取 sec_uid / uid / 昵称（权威数据，不靠昵称猜）。

产出（默认写到同目录 export/ 下）：
  1. 收藏视频明细_<时间>.csv     每个收藏视频一行
  2. 收藏博主汇总_<时间>.csv     按 sec_uid 去重，含收藏数 / 是否已下载 / 主页链接
  3. 未下载博主_主页链接_<时间>.txt  可直接粘给 DouK-Downloader 批量下载
  4. 收藏原始数据_<时间>.jsonl   逐页原始 JSON，断点续跑用

用法：
  python fetch_favorites_authors.py               # 全量抓取
  python fetch_favorites_authors.py --max-pages 3 # 只抓前 3 页试水
  python fetch_favorites_authors.py --count 20    # 每页条数（默认 20）
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from urllib.parse import urlencode

# ---------------- 配置 ----------------
DOWNLOADER_DIR = r"E:\dyyyyyyyyyyyyydownload\TikTokDownloader_V5.6_Windows_X64\DouK-Downloader_V5.8_Windows_X64"
LIB_DIR = os.path.join(DOWNLOADER_DIR, "lib")
SETTINGS = os.path.join(DOWNLOADER_DIR, "Volume", "settings.json")

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

API_URL = "https://www.douyin.com/aweme/v1/web/aweme/listcollection/"
REFERER = "https://www.douyin.com/user/self?showTab=favorite_collection"

SOURCES = {
    "collect": ("https://www.douyin.com/aweme/v1/web/aweme/listcollection/",
                "https://www.douyin.com/user/self?showTab=favorite_collection", "收藏"),
    "like": ("https://www.douyin.com/aweme/v1/web/aweme/favorite/",
             "https://www.douyin.com/user/self?showTab=like", "喜欢"),
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")


def init_env():
    """把 DouK 自带的 curl_cffi / 源码目录挂到 sys.path。"""
    os.add_dll_directory(os.path.abspath(LIB_DIR))
    p = os.path.join(LIB_DIR, "curl_cffi.libs")
    if os.path.isdir(p):
        os.add_dll_directory(os.path.abspath(p))
    if LIB_DIR not in sys.path:
        sys.path.insert(0, os.path.abspath(LIB_DIR))
    os.chdir(DOWNLOADER_DIR)


def load_ctx():
    st = json.load(open(SETTINGS, encoding="utf-8-sig"))
    ck = st.get("cookie") or {}
    if not ck.get("sessionid"):
        sys.exit("[!] settings.json 里没有有效 sessionid，请先在 DouK-Downloader 里更新 Cookie（选 1 或 2）")
    return st, ck


def build_query(bi):
    return {
        "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
        "pc_client_type": "1", "version_code": "170400", "version_name": "17.4.0",
        "cookie_enabled": "true", "screen_width": "1536", "screen_height": "864",
        "browser_language": bi.get("browser_language", "zh-CN"),
        "browser_platform": bi.get("browser_platform", "MacIntel"),
        "browser_name": bi.get("browser_name", "Chrome"),
        "browser_version": bi.get("browser_version", "146.0.0.0"),
        "browser_online": "true",
        "engine_name": bi.get("engine_name", "Blink"),
        "engine_version": bi.get("engine_version", "146.0.0.0"),
        "os_name": bi.get("os_name", "Mac OS"),
        "os_version": bi.get("os_version", "10.15.7"),
        "cpu_core_num": "8", "device_memory": "8", "platform": "PC",
        "downlink": "10", "effective_type": "4g", "round_trip_time": "50",
    }


def make_headers(ck, referer=REFERER):
    h = {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.douyin.com",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "x-tt-argus": "1",              # 关键：缺这个会被 ArgusSecurityPlugin 拦成 403
        "uifid": str(ck.get("UIFID") or ""),  # 关键：缺这个报 Uifid Not Found
        "Cookie": "; ".join(f"{k}={v}" for k, v in ck.items() if v not in (None, "")),
    }
    return h


def fetch_page(cr, ab, ck, bi, cursor, count, retry=3, url=API_URL, referer=REFERER):
    body = urlencode({"count": count, "cursor": cursor})
    last = ""
    for attempt in range(1, retry + 1):
        try:
            q = build_query(bi)
            abogus = ab.get_value(urlencode(q), body=body,
                                  content_type="application/x-www-form-urlencoded")
            q["a_bogus"] = abogus
            r = cr.post(url, params=q, data=body, headers=make_headers(ck, referer),
                        impersonate="chrome146", timeout=30)
            if r.status_code != 200:
                last = f"HTTP {r.status_code}: {r.text[:120]}"
                time.sleep(attempt * 1.5)
                continue
            d = r.json()
            code = d.get("status_code")
            if code not in (0, None):
                last = f"status_code={code} {d.get('status_msg')}"
                time.sleep(attempt * 1.5)
                continue
            return d
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(attempt * 1.5)
    raise RuntimeError(f"抓取失败（cursor={cursor}，重试 {retry} 次）：{last}")


def parse_aweme(a):
    au = a.get("author") or {}
    stats = a.get("statistics") or {}
    ct = a.get("create_time")
    return {
        "aweme_id": a.get("aweme_id"),
        "desc": (a.get("desc") or "").replace("\n", " ").strip(),
        "create_time": datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M") if ct else "",
        "media_type": "图文" if a.get("images") else "视频",
        "duration_s": round((a.get("video") or {}).get("duration", 0) / 1000, 1) or "",
        "digg_count": stats.get("digg_count", ""),
        "comment_count": stats.get("comment_count", ""),
        "collect_count": stats.get("collect_count", ""),
        "sec_uid": au.get("sec_uid", ""),
        "uid": au.get("uid", ""),
        "nickname": au.get("nickname", ""),
        "douyin_id": au.get("unique_id") or au.get("short_id") or "",
        "aweme_url": f"https://www.douyin.com/video/{a.get('aweme_id')}",
        "author_url": f"https://www.douyin.com/user/{au.get('sec_uid', '')}",
    }


def load_known():
    """已下载过的作者：uid -> 备注，sec_uid -> 备注。"""
    known_uid, known_sec = {}, {}
    for path in (DATA_JSON, USER_DATA):
        if not os.path.isfile(path):
            continue
        try:
            d = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        for uid, v in (d.get("accounts") or {}).items():
            name = v.get("nickname") or ""
            known_uid[str(uid)] = name
            if v.get("sec_uid"):
                known_sec[v["sec_uid"]] = name
    # 现有对照表
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
    ap.add_argument("--max-pages", type=int, default=0, help="最多抓多少页（0=全部）")
    ap.add_argument("--count", type=int, default=20, help="每页条数")
    ap.add_argument("--delay", type=float, default=0.8, help="每页间隔秒")
    ap.add_argument("--source", choices=["collect", "like"], default="collect",
                    help="collect=收藏（默认）, like=喜欢(点赞)")
    args = ap.parse_args()

    init_env()
    from curl_cffi import requests as cr
    from src.encrypt.aBogus import ABogus

    st, ck = load_ctx()
    bi = st.get("browser_info") or {}
    ab = ABogus(user_agent=UA)

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    api_url, referer, label = SOURCES[args.source]
    f_detail = os.path.join(OUT_DIR, f"{label}视频明细_{stamp}.csv")
    f_authors = os.path.join(OUT_DIR, f"{label}博主汇总_{stamp}.csv")
    f_pending = os.path.join(OUT_DIR, f"未下载博主_主页链接_{stamp}.txt")
    f_raw = os.path.join(OUT_DIR, f"{label}原始数据_{stamp}.jsonl")

    print(f"[开始] 逐页抓取「{label}」列表（每页 {args.count} 条）…")
    rows, seen_aweme = [], set()
    cursor, page, total_raw = "0", 0, 0
    raw_fp = open(f_raw, "w", encoding="utf-8")

    while True:
        page += 1
        d = fetch_page(cr, ab, ck, bi, cursor, args.count, url=api_url, referer=referer)
        al = d.get("aweme_list") or []
        raw_fp.write(json.dumps(d, ensure_ascii=False) + "\n")
        raw_fp.flush()
        total_raw += len(al)
        for a in al:
            aid = a.get("aweme_id")
            if not aid or aid in seen_aweme:
                continue
            seen_aweme.add(aid)
            rows.append(parse_aweme(a))
        has_more = bool(d.get("has_more"))
        nxt = str(d.get("cursor") or "")
        print(f"  第 {page} 页 · 本页 {len(al)} 条 · 累计 {len(rows)} 条 · has_more={has_more}")
        if not has_more or not al or nxt in ("", "0") or nxt == cursor:
            break
        cursor = nxt
        if args.max_pages and page >= args.max_pages:
            print(f"  （已到 --max-pages {args.max_pages}，停止）")
            break
        time.sleep(args.delay)

    raw_fp.close()
    if not rows:
        sys.exit("[!] 一条都没抓到：可能收藏为空，或 Cookie 已失效")

    # ---- 明细 CSV ----
    cols = list(rows[0].keys())
    with open(f_detail, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # ---- 按作者汇总 ----
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

    # 黑名单剔除：不下载的博主，收藏/喜欢里出现也一律不要
    sys.path.insert(0, SCRIPT_DIR)
    import blacklist as BL
    dropped = []
    for k in list(authors):
        a = authors[k]
        if BL.get().match(sec_uid=a["sec_uid"], uid=a["uid"], nickname=a["nickname"]):
            dropped.append(authors.pop(k))
    if dropped:
        print(f"[黑名单] 已剔除 {len(dropped)} 个不下载的博主：" +
              "、".join((d["nickname"] or d["uid"]) for d in dropped[:6]))

    out = []
    for a in authors.values():
        downloaded = (a["uid"] in known_uid) or (a["sec_uid"] in known_sec)
        a["downloaded"] = "已下载" if downloaded else "未下载"
        a["known_as"] = known_uid.get(a["uid"]) or known_sec.get(a["sec_uid"]) or ""
        a["last_work"] = a.pop("_latest")
        out.append(a)
    out.sort(key=lambda x: (-x["count"], x["nickname"]))

    acols = ["sec_uid", "uid", "nickname", "douyin_id", "count", "last_work",
             "downloaded", "known_as", "author_url", "sample_aweme"]
    with open(f_authors, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=acols, extrasaction="ignore")
        w.writeheader()
        w.writerows(out)

    # ---- 未下载博主主页链接（可直接喂 DouK 批量下载）----
    pending = [a for a in out if a["downloaded"] == "未下载" and a["sec_uid"]]
    with open(f_pending, "w", encoding="utf-8") as f:
        for a in pending:
            f.write(a["author_url"] + "\n")

    # ---- 打印概览 ----
    print("\n" + "=" * 56)
    print(f"收藏视频总数：{len(rows)} 条（去重后）")
    print(f"涉及博主数  ：{len(out)} 个")
    print(f"  已下载过  ：{len(out) - len(pending)} 个")
    print(f"  尚未下载  ：{len(pending)} 个")
    print("=" * 56)
    print(f"[明细] {f_detail}")
    print(f"[汇总] {f_authors}")
    print(f"[待下载链接] {f_pending}")
    print(f"[原始] {f_raw}")


if __name__ == "__main__":
    main()
