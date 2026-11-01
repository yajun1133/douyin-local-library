#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""子进程助手：抓抖音「收藏 / 喜欢」列表里的作者（必须用 Python 3.12 跑）。

为什么独立脚本：下载器 lib/ 里的 curl_cffi 和 ABogus 签名都是 3.12 编译版，
服务端跑 3.13 import 会报 bad magic number。所以 server.py 用 C:\\Python312\\python.exe
拉起本脚本，stdin 传一行 JSON，stdout 最后一行输出 JSON 结果。

复用 fetch_favorites_authors.py 的抓取逻辑（listcollection / favorite 接口 + ABogus 签名）。

协议（一行一 JSON）：
    输入 {"count": 30, "source": "collect"}   # count=要抓的收藏条数，source=collect|like
    输出 {"ok": true, "authors": [{sec_uid,uid,nickname,douyin_id,count,last_work,aweme_id}], "total": 去重后的视频条数}
     或  {"ok": false, "error": "..."}
"""

import json
import os
import sys
import time
from datetime import datetime
from urllib.parse import urlencode

# 路径统一来自 ../src/paths.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

# 组件地址（下载器目录）若服务端通过环境变量传了覆盖值，优先用（对应设置里的「组件地址」）
DOWNLOADER_DIR = os.environ.get("DOUK_DOWNLOADER_DIR") or paths.DOWNLOADER_DIR
LIB_DIR = os.path.join(DOWNLOADER_DIR, "lib")
SETTINGS = os.path.join(DOWNLOADER_DIR, "Volume", "settings.json")

SOURCES = {
    "collect": ("https://www.douyin.com/aweme/v1/web/aweme/listcollection/",
                "https://www.douyin.com/user/self?showTab=favorite_collection"),
    "like": ("https://www.douyin.com/aweme/v1/web/aweme/favorite/",
             "https://www.douyin.com/user/self?showTab=like"),
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")


def init_env():
    os.add_dll_directory(os.path.abspath(LIB_DIR))
    p = os.path.join(LIB_DIR, "curl_cffi.libs")
    if os.path.isdir(p):
        os.add_dll_directory(os.path.abspath(p))
    if LIB_DIR not in sys.path:
        sys.path.insert(0, os.path.abspath(LIB_DIR))
    # aBogus 在 lib/src/encrypt 下（下载器打包后源码收在 lib/ 里）
    src_dir = os.path.join(LIB_DIR, "src")
    if os.path.isdir(src_dir) and src_dir not in sys.path:
        sys.path.insert(0, src_dir)
    os.chdir(DOWNLOADER_DIR)


def load_ctx():
    if not os.path.isfile(SETTINGS):
        return None, {}
    try:
        st = json.load(open(SETTINGS, encoding="utf-8-sig"))
    except Exception:
        return None, {}
    ck = st.get("cookie") or {}
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


def make_headers(ck, referer):
    return {
        "User-Agent": UA,
        "Referer": referer,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.douyin.com",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Content-Type": "application/x-www-form-urlencoded",
        "x-tt-argus": "1",
        "uifid": str(ck.get("UIFID") or ""),
        "Cookie": "; ".join(f"{k}={v}" for k, v in ck.items() if v not in (None, "")),
    }


def fetch_page(cr, ab, ck, bi, cursor, count, api_url, referer, retry=3):
    body = urlencode({"count": count, "cursor": cursor})
    last = ""
    for attempt in range(1, retry + 1):
        try:
            q = build_query(bi)
            abogus = ab.get_value(urlencode(q), body=body,
                                  content_type="application/x-www-form-urlencoded")
            q["a_bogus"] = abogus
            r = cr.post(api_url, params=q, data=body, headers=make_headers(ck, referer),
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
    ct = a.get("create_time")
    return {
        "aweme_id": a.get("aweme_id"),
        "create_time": datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M") if ct else "",
        "desc": (a.get("desc") or "").strip(),
        "sec_uid": au.get("sec_uid", ""),
        "uid": str(au.get("uid", "")) if au.get("uid") is not None else "",
        "nickname": au.get("nickname", ""),
        "douyin_id": au.get("unique_id") or au.get("short_id") or "",
    }


def public_aweme_count(sec_uid, timeout=10):
    """公开接口（**不需要登录**）读 aweme_count：用来判断 aweme/post 返回空列表
    到底是「对方真的没有公开作品」还是「被风控挡了」。

    返回：
        int >= 0  → 抖音对外可见的作品数（0 = 一个都不公开）
        -1        → 这个 sec_uid 已经失效（账号注销/换号，抖音只回一个
                     `用户5742…` 占位资料，连 uid/sec_uid 都没有）
        None      → 接口没通/拿不到，无从判断

    这个接口不消耗登录态额度，失败也不影响主流程。
    """
    if not sec_uid:
        return None
    import urllib.request
    url = ("https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid="
           + urllib.parse.quote(sec_uid))
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
        "Referer": "https://www.douyin.com/",
    })
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace")
        d = json.loads(raw)
        if d.get("status_code") == 0:
            ui = d.get("user_info") or {}
            cnt = ui.get("aweme_count")
            if isinstance(cnt, int):
                return cnt
            # 占位资料：昵称是「用户+数字」、没有 uid/sec_uid —— 账号没了
            if not ui.get("uid") and not ui.get("sec_uid"):
                return -1
    except Exception:
        pass
    return None


def fetch_author_latest(cr, ab, ck, bi, sec_uid, retry=2):
    """查作者主页作品列表（aweme/post），拿最新一条作品的发布时间。

    返回 "YYYY-MM-DD HH:MM" 或 ""（拿不到）。收藏接口只给「你收藏的那条」的时间，
    作者真正的「最新作品」得靠这里翻主页第一条。
    """
    if not sec_uid:
        return ""
    last = ""
    for attempt in range(1, retry + 1):
        try:
            q = build_query(bi)
            q["sec_user_id"] = sec_uid
            q["count"] = "18"
            q["max_cursor"] = "0"
            abogus = ab.get_value(urlencode(q), body="",
                                  content_type="application/x-www-form-urlencoded")
            q["a_bogus"] = abogus
            r = cr.get("https://www.douyin.com/aweme/v1/web/aweme/post/",
                       params=q, headers=make_headers(ck, "https://www.douyin.com/"),
                       impersonate="chrome146", timeout=20)
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
                time.sleep(attempt * 1.2)
                continue
            d = r.json()
            if d.get("status_code") not in (0, None):
                last = f"status={d.get('status_code')}"
                time.sleep(attempt * 1.2)
                continue
            al = d.get("aweme_list") or []
            if not al:
                return ""
            # 不能取 al[0]：抖音会把「置顶」的作品排在最前面，置顶往往是老作品，
            # 那样就会算出「抖音最新发布 2026-09-11」而本地明明下到 09-16。取整页最大时间。
            ct = max((a.get("create_time") or 0) for a in al)
            if ct:
                return datetime.fromtimestamp(ct).strftime("%Y-%m-%d %H:%M")
            return ""
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            time.sleep(attempt * 1.2)
    return ""


def run(req):
    count = int(req.get("count") or 20)
    source = str(req.get("source") or "collect").strip()
    if source not in SOURCES:
        source = "collect"
    # 「最新作品时间」口径：none=不查(用收藏作品时间) / downloaded=只查已下载 / all=都查
    latest_mode = str(req.get("latest_mode") or "none").strip()
    if latest_mode not in ("none", "downloaded", "all"):
        latest_mode = "none"
    known_sec = set(req.get("known_sec") or [])   # 已下载作者的 sec_uid 集合（server 传进来）
    api_url, referer = SOURCES[source]

    if not os.path.isdir(LIB_DIR):
        return {"ok": False, "error": "找不到下载器 lib 目录（curl_cffi），请确认组件地址设置正确"}
    init_env()
    from curl_cffi import requests as cr  # noqa: E402

    try:
        from src.encrypt.aBogus import ABogus  # noqa: E402
    except ImportError:
        from encrypt.aBogus import ABogus  # noqa: E402

    st, ck = load_ctx()
    if not ck or not ck.get("sessionid"):
        return {"ok": False, "error": "下载器 settings.json 里没有有效登录 cookie（sessionid 为空）"}
    bi = st.get("browser_info") or {}
    ab = ABogus(user_agent=UA)

    # 抓够 count 条（count 是「条数」不是「页数」；每页最多 20 条）
    rows = []
    seen = set()
    cursor = "0"
    max_pages = max(1, (count + 19) // 20)
    per_page = min(20, count)
    for page in range(max_pages):
        d = fetch_page(cr, ab, ck, bi, cursor, per_page, api_url, referer)
        al = d.get("aweme_list") or []
        for a in al:
            aid = a.get("aweme_id")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            rows.append(parse_aweme(a))
            if len(rows) >= count:
                break
        if len(rows) >= count:
            break
        has_more = bool(d.get("has_more"))
        nxt = str(d.get("cursor") or "")
        if not has_more or not al or nxt in ("", "0") or nxt == cursor:
            break
        cursor = nxt
        time.sleep(0.8)

    if not rows:
        return {"ok": False, "error": "一条都没抓到：可能收藏为空，或 Cookie 已失效"}

    # 按作者去重（按 sec_uid 优先，其次 uid）
    authors = {}
    for r in rows:
        k = r["sec_uid"] or ("uid_" + r["uid"]) if r["uid"] else None
        if not k:
            continue
        a = authors.setdefault(k, {
            "sec_uid": r["sec_uid"], "uid": r["uid"], "nickname": r["nickname"],
            "douyin_id": r["douyin_id"], "count": 0, "last_work": "",
            "aweme_id": r["aweme_id"], "latest_desc": "",
        })
        a["count"] += 1
        if r["create_time"] > a["last_work"]:
            a["last_work"] = r["create_time"]
            a["latest_desc"] = r.get("desc") or ""
            a["nickname"] = r["nickname"] or a["nickname"]
        if not a["douyin_id"] and r["douyin_id"]:
            a["douyin_id"] = r["douyin_id"]

    # 「最新作品时间」：按口径对相应作者翻主页拿真实最新发布时间（覆盖 last_work）
    if latest_mode in ("downloaded", "all"):
        for a in authors.values():
            sec = a["sec_uid"]
            if not sec:
                continue
            # downloaded 口径只查「已下载」作者（server 传来的 known_sec）；all 全查
            if latest_mode == "downloaded" and sec not in known_sec:
                continue
            t = fetch_author_latest(cr, ab, ck, bi, sec)
            if t:
                a["latest_post_time"] = t
            time.sleep(0.5)   # 逐个查，控制节奏，降低风控

    out = sorted(authors.values(), key=lambda x: (-x["count"], x["nickname"]))
    return {"ok": True, "authors": out, "total": len(rows), "source": source,
            "latest_mode": latest_mode}


def main():
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        pass
    try:
        req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        print(json.dumps({"ok": False, "error": "bad input"}, ensure_ascii=True))
        return
    try:
        resp = run(req)
    except Exception as e:
        resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    print(json.dumps(resp, ensure_ascii=True))


if __name__ == "__main__":
    main()
