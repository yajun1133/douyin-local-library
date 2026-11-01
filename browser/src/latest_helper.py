#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻子进程（必须用 Python 3.12）：联网查「作者最新一条作品的发布时间」。

为什么要独立进程：抖音的 `aweme/v1/web/aweme/post/` 现在被 Argus 拦（返回
`Blocked by ArgusSecurityPlugin Uifid Not Found / Signature Not Found`），必须带
UIFID 头 + a_bogus 签名 + 浏览器 TLS 指纹。a_bogus 和 curl_cffi 都是下载器 lib/ 里
**3.12 编译版**，服务端跑 3.13 import 会 bad magic number，所以外包给这个进程。

协议：stdin 一行一个 JSON，stdout 一行一个 JSON：
    输入 {"uid": "...", "sec_uid": "..."}
    输出 {"ok": true, "uid": "...", "latest": "2026-10-28 16:08", "ts": 1793000000}
     或  {"ok": false, "uid": "...", "error": "..."}

复用 fetch_favorites.py 里已经跑通的签名链路（ABogus + curl_cffi + 下载器 cookie），
不重复造轮子。
"""

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fetch_favorites as ff  # noqa: E402


def main():
    ff.init_env()
    from curl_cffi import requests as cr  # noqa: E402
    try:
        from src.encrypt.aBogus import ABogus  # noqa: E402
    except ImportError:
        from encrypt.aBogus import ABogus  # noqa: E402

    st, ck = ff.load_ctx()
    if not ck or not ck.get("sessionid"):
        print(json.dumps({"ok": False, "error": "下载器 settings.json 里没有有效登录 cookie"}),
              flush=True)
        return
    bi = st.get("browser_info") or {}
    ab = ABogus(user_agent=ff.UA)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            print(json.dumps({"ok": False, "error": "bad json"}), flush=True)
            continue
        uid = str(req.get("uid") or "")
        sec = str(req.get("sec_uid") or "").strip()
        if not sec:
            print(json.dumps({"ok": False, "uid": uid, "error": "没有 SEC_UID"}, ensure_ascii=False),
                  flush=True)
            continue
        try:
            latest = ff.fetch_author_latest(cr, ab, ck, bi, sec)
        except Exception as e:
            print(json.dumps({"ok": False, "uid": uid, "error": "%s: %s" % (type(e).__name__, e)},
                             ensure_ascii=False), flush=True)
            continue
        ts = 0
        if latest:
            try:
                ts = int(datetime.strptime(latest, "%Y-%m-%d %H:%M").timestamp())
            except ValueError:
                ts = 0
        out = {"ok": bool(latest), "uid": uid, "latest": latest, "ts": ts, "error": ""}
        if not latest:
            # 空列表有两种完全不同的原因，必须分开，不然后台批量会把「一批私密号」
            # 当成「被限流」直接熔断：
            #   aweme_count == 0 → 对方的作品本来就对外不可见（私密/限流/封禁/全删），
            #                      属于「没得查」，不该算失败；
            #   aweme_count == -1 → 这个 sec_uid 已经失效（账号注销/换号），要去重新补；
            #   aweme_count  > 0 → 主页明明有作品却返回空，这才像被风控挡了。
            cnt = ff.public_aweme_count(sec)
            if cnt == 0:
                out["empty"] = True
                out["error"] = "抖音上这个号的作品对外不可见（公开作品数为 0）"
            elif cnt == -1:
                out["dead"] = True
                out["error"] = "SEC_UID 已失效（账号注销或换了号），需要重新补 SEC_UID"
            elif isinstance(cnt, int) and cnt > 0:
                out["error"] = ("主页有 %d 个公开作品但接口返回空列表，可能被风控挡了，"
                                "过一会儿再试" % cnt)
            else:
                out["error"] = "接口没返回作品（可能私密/无作品/被限流）"
        print(json.dumps(out, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
