#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
子进程助手：批量 sec_uid -> uid / 昵称 / 抖音号（供 server.py 调用）。

为什么是独立脚本：下载器 lib 里的 curl_cffi 是 CPython 3.12 的编译版，而服务端
跑在 3.13 上（import 报 bad magic number）。所以 server.py 用 C:\\Python312\\python.exe
拉起本脚本，stdin 传 {"sec_uids": [...]}，stdout 最后一行输出 JSON：
    {sec_uid: {"uid","nickname","unique_id","short_id"}}
复用 fetch_uid_by_secuid.py 里的环境初始化、cookie 加载和批量查询。
"""

import sys
import json

# fetch_uid_by_secuid.py 在 c27 目录重整时被归到「tools/legacy/」，但本脚本仍复用它
# 里的环境初始化与 cookie 读取。子进程的 sys.path[0] 只有 src/，直接 import 会
# ModuleNotFoundError（traceback 走 stderr，stdout 空 → server 只看到「查询进程无输出」，
# 表现成「新增作者」一直失败）。所以这里显式把两个目录加进 sys.path。
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "tools", "legacy"), _HERE):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)


def main():
    try:
        req = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        print("{}")
        return
    secs = [s for s in (req.get("sec_uids") or []) if isinstance(s, str) and s]
    if not secs:
        print("{}")
        return

    try:
        import fetch_uid_by_secuid as fk
    except Exception as e:
        # 明确报出来，别再让上层只能猜「cookie 过期」
        print(json.dumps({"__error": "缺少 fetch_uid_by_secuid.py: %s" % e}, ensure_ascii=True))
        return
    fk.init_env()
    from curl_cffi import requests as cr

    cookie = fk.load_cookie()
    if not cookie.get("sessionid"):
        # 没有 cookie 时输出错误标记，server 端据此报「cookie 过期」
        print(json.dumps({"__error": "no_cookie"}, ensure_ascii=True))
        return

    out = {}
    BATCH = 20
    for i in range(0, len(secs), BATCH):
        try:
            out.update(fk.query_batch(cr, cookie, secs[i:i + BATCH]))
        except Exception as e:
            out["__error"] = str(e)
    print(json.dumps(out, ensure_ascii=True))


if __name__ == "__main__":
    main()
