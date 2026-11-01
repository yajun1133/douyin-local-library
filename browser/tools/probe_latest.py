#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：打印某个作者主页 aweme/post 的原始返回，用来核对「最新发布时间」口径。

必须用 3.12 跑（curl_cffi / ABogus 是 3.12 编译版）：
    C:\\Python312\\python.exe tools\\probe_latest.py <uid 或 sec_uid> [--pages N] [--find <aweme_id>]

关注三件事：
  1. 页里有没有 is_top=1（置顶）——有的话 al[0] 就不是最新，必须取整页 max(create_time)
  2. --find 给的 aweme_id 在不在列表里（判断「本地有、接口没有」是接口没翻到还是对方删了/设私密）
  3. 每页 has_more / cursor，决定要翻几页才能覆盖
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)
import fetch_favorites as ff  # noqa: E402


def fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("who", help="uid（纯数字）或 sec_uid（MS4wLjAB...）")
    ap.add_argument("--pages", type=int, default=3, help="最多翻几页（每页 18 条）")
    ap.add_argument("--find", default="", help="要在这份列表里找的 aweme_id")
    ap.add_argument("--json", action="store_true", help="额外输出完整原始 JSON 行")
    args = ap.parse_args()

    ff.init_env()
    from curl_cffi import requests as cr
    try:
        from src.encrypt.aBogus import ABogus
    except ImportError:
        from encrypt.aBogus import ABogus

    st, ck = ff.load_ctx()
    if not ck or not ck.get("sessionid"):
        print("× 下载器 settings.json 里没有有效登录 cookie")
        return 2
    bi = st.get("browser_info") or {}
    ab = ABogus(user_agent=ff.UA)

    sec = args.who.strip()
    if sec.isdigit():
        # uid → sec_uid：从本地扫描数据里查
        p = os.path.join(os.path.dirname(SRC), "data", "data.json")
        d = json.load(open(p, encoding="utf-8"))
        a = (d.get("accounts") or {}).get(sec) or {}
        sec = str(a.get("sec_uid") or "").strip()
        if not sec:
            print("× 本地数据里没有这个 uid 的 SEC_UID")
            return 2
        print("作者：%s  uid=%s" % (a.get("nickname"), args.who))

    all_items = []
    cursor = "0"
    for page in range(1, args.pages + 1):
        q = ff.build_query(bi)
        q["sec_user_id"] = sec
        q["count"] = "18"
        q["max_cursor"] = cursor
        abogus = ab.get_value(ff.urlencode(q), body="",
                             content_type="application/x-www-form-urlencoded")
        q["a_bogus"] = abogus
        r = cr.get("https://www.douyin.com/aweme/v1/web/aweme/post/", params=q,
                   headers=ff.make_headers(ck, "https://www.douyin.com/"),
                   impersonate="chrome146", timeout=25)
        print("\n--- 第 %d 页  HTTP %s ---" % (page, r.status_code))
        if r.status_code != 200:
            print(r.text[:300])
            return 1
        d = r.json()
        if d.get("status_code") not in (0, None):
            print("status_code=%s %s" % (d.get("status_code"), d.get("status_msg")))
            return 1
        al = d.get("aweme_list") or []
        print("条数 %d  has_more=%s  cursor=%s → %s"
              % (len(al), d.get("has_more"), cursor, d.get("max_cursor")))
        for a in al:
            ct = a.get("create_time") or 0
            top = a.get("is_top")
            all_items.append({"aweme_id": str(a.get("aweme_id") or ""), "ct": ct,
                              "is_top": top, "desc": (a.get("desc") or "")[:36]})
            print("  %s  %s%s  %s" % (fmt(ct), a.get("aweme_id"),
                                      "  【置顶 is_top=%s】" % top if top else "",
                                      (a.get("desc") or "").replace("\n", " ")[:40]))
        if not al or not d.get("has_more"):
            break
        nxt = str(d.get("max_cursor") or "")
        if not nxt or nxt == cursor:
            break
        cursor = nxt
        time.sleep(1.0)

    print("\n=== 汇总（共 %d 条）===" % len(all_items))
    if not all_items:
        return 0
    mx = max(all_items, key=lambda x: x["ct"])
    first = all_items[0]
    print("al[0]      : %s  %s" % (fmt(first["ct"]), first["aweme_id"]))
    print("整页最大值 : %s  %s" % (fmt(mx["ct"]), mx["aweme_id"]))
    print("首条是否就是最新：%s" % ("是" if first is mx else "**否 —— 取 al[0] 会算错**"))
    tops = [x for x in all_items if x["is_top"]]
    print("置顶条数   : %d %s" % (len(tops), [x["aweme_id"] for x in tops]))
    if args.find:
        f = [x for x in all_items if x["aweme_id"] == args.find.strip()]
        print("要找的 %s：%s" % (args.find.strip(),
                               ("在列表里，发布 " + fmt(f[0]["ct"])) if f else "**不在列表里**"))
    if args.json:
        print(json.dumps(all_items, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
