#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""常驻的抖音资料查询助手（必须用 Python 3.12 跑）。

为什么要有它：`iesdouyin.com/web/api/v2/user/info/` 这个公开接口现在把
`signature`（简介）返回成空串（2026-10-27 实测），所以「获取资料」拿不到简介。
登录态接口 `aweme/v1/web/user/profile/other/?user_id=` 能拿到完整 user 对象
（nickname / unique_id / signature / sec_uid / follower_count ...）。

但它依赖 curl_cffi，而下载器 lib/ 里的 curl_cffi 是 3.12 编译版，服务端跑在 3.13 上
import 会报 bad magic number —— 所以这里起一个常驻子进程，服务端用 stdin/stdout
一行一个 JSON 跟它对话，避免每次请求重新启动解释器（启动一次要 1~2 秒）。

协议（一行一 JSON）：
    输入 {"uid": "...", "sec_uid": "..."}   # 两个给一个就行，优先 uid
    输出 {"ok": true, "uid","sec_uid","nickname","douyin_id","signature","follower_count","aweme_count"}
     或  {"ok": false, "error": "..."}
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# fetch_uid_by_secuid 后来挪到 tools/legacy/ 了，这里两种位置都试一下，
# 否则 import 直接挂、整条「获取资料」链路不可用（2026-11-04 踩到）。
_LEGACY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "legacy")
if os.path.isdir(_LEGACY):
    sys.path.insert(0, _LEGACY)
import fetch_uid_by_secuid as F

F.init_env()
from curl_cffi import requests as cr   # noqa: E402  （必须 init_env 之后导入）

PROFILE_URL = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
IM_URL = "https://www.douyin.com/aweme/v1/web/im/user/info/"
BASE_PARAMS = {
    "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
    "version_code": "170400", "version_name": "17.4.0",
}
HEADERS = {"User-Agent": F.UA, "Referer": "https://www.douyin.com/"}
COOKIE = F.load_cookie()


def _get(url, params):
    r = cr.get(url, params=params, headers=HEADERS, cookies=COOKIE,
               impersonate="chrome131", timeout=15)
    return json.loads(r.text)


def sec_to_uid(sec_uid):
    """sec_uid → uid（登录态批量接口，单个也能查）"""
    r = cr.post(IM_URL, params=BASE_PARAMS,
                data={"sec_user_ids": json.dumps([sec_uid])},
                headers=HEADERS, cookies=COOKIE, impersonate="chrome131", timeout=15)
    for u in (json.loads(r.text).get("data") or []):
        if u.get("uid"):
            return str(u["uid"])
    return ""


def handle(req):
    uid = str(req.get("uid") or "").strip()
    sec_uid = str(req.get("sec_uid") or "").strip()
    if not uid and sec_uid:
        uid = sec_to_uid(sec_uid)
    if not uid:
        return {"ok": False, "error": "没拿到 uid（sec_uid 可能已失效，或账号已注销）"}
    d = _get(PROFILE_URL, dict(BASE_PARAMS, user_id=uid))
    u = d.get("user") or {}
    if not u:
        return {"ok": False, "error": "接口没返回 user（账号可能已注销/私密/被限制）", "uid": uid}
    return {
        "ok": True,
        "uid": str(u.get("uid") or uid),
        "sec_uid": u.get("sec_uid") or sec_uid,
        "nickname": u.get("nickname") or "",
        "douyin_id": u.get("unique_id") or u.get("short_id") or "",
        "signature": u.get("signature") or "",
        "follower_count": u.get("follower_count"),
        "aweme_count": u.get("aweme_count"),
    }


def main():
    # Windows 上 stdout 被重定向（管道）时默认用系统 ANSI 编码（GBK），
    # 服务端按 UTF-8 读就会整片变成 ���。这里强制两个方向都用 UTF-8。
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    except Exception:
        pass          # 老版本没有 reconfigure 就靠下面的 ensure_ascii 兜底
    if not COOKIE.get("sessionid"):
        # 不是致命错误：接口有时不带 cookie 也能过，只是更容易被风控
        sys.stderr.write("[warn] 没读到登录 cookie\n")
    sys.stderr.write("[ready] profile helper started\n")
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            resp = {"ok": False, "error": "bad json"}
        else:
            try:
                resp = handle(req)
            except Exception as e:      # 单个账号失败不能拖垮整个进程
                resp = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        # ensure_ascii=True：中文写成 \uXXXX，纯 ASCII 过管道，
        # 就算子进程编码没配对也不会出现乱码（服务端 json.loads 会自动还原）
        sys.stdout.write(json.dumps(resp, ensure_ascii=True) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
