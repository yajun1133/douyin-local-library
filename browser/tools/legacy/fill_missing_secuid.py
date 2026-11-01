#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 UID 反查 SEC_UID，补掉 data.json 里缺 sec_uid 的账号。

抖音没有公开的「uid → sec_uid」接口，但登录态下请求
    GET https://www.douyin.com/aweme/v1/web/user/profile/other/?user_id=<uid>
返回的 user 对象里有 sec_uid（这是 2026-10-27 补号时验证过的反向链路）。

只处理「data.json 和 user_data.json 里都没有 sec_uid」的账号；写盘不直接改文件，
而是 POST /api/save 走服务端的 merge_save_user_data（合并写 + 并发锁 + 原子替换）。

用法:
    python fill_missing_secuid.py --dry     # 只查不写，看能补几个
    python fill_missing_secuid.py           # 查询并回填（先自动备份 user_data.json）

依赖（与 fetch_uid_by_secuid.py 相同，必须用 Python 3.12 跑）:
    E:\\...\\DouK-Downloader_V5.8_Windows_X64\\lib\\                  curl_cffi + DLL
    E:\\...\\DouK-Downloader_V5.8_Windows_X64\\Volume\\settings.json  登录 cookie
"""

import json
import os
import shutil
import sys
import time
import urllib.request

import fetch_uid_by_secuid as F

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
import sys  # noqa: E402
sys.path.insert(0, _TOOLS)                       # tools/ 下的 blacklist、downloaded
sys.path.insert(0, os.path.join(_ROOT, "src"))   # src/paths.py 统一路径
import paths  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # 仅用于拼本目录内的东西
DATA_JSON = os.path.join(SCRIPT_DIR, "data.json")
USER_DATA = paths.USER_DATA
BACKUP_DIR = paths.BACKUP_DIR
API = "http://127.0.0.1:8723"

PROFILE_URL = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
PARAMS = {
    "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
    "version_code": "170400", "version_name": "17.4.0",
}


def load_json(path):
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def missing_uids():
    """data.json 和 user_data.json 都没有 sec_uid 的账号 → [(uid, nickname)]"""
    dacc = load_json(DATA_JSON).get("accounts") or {}
    uacc = load_json(USER_DATA).get("accounts") or {}
    out = []
    for uid, a in dacc.items():
        if a.get("sec_uid") or (uacc.get(uid) or {}).get("sec_uid"):
            continue
        out.append((uid, a.get("nickname", "")))
    return out, uacc


def query_sec_uid(cr, cookie, uid):
    """uid → (sec_uid, nickname)；查不到返回 ("", "")。"""
    try:
        resp = cr.get(PROFILE_URL, params=dict(PARAMS, user_id=uid),
                      headers={"User-Agent": F.UA, "Referer": "https://www.douyin.com/"},
                      cookies=cookie, impersonate="chrome131", timeout=30)
        user = (json.loads(resp.text) or {}).get("user") or {}
        return (user.get("sec_uid") or ""), (user.get("nickname") or "")
    except Exception as e:
        print(f"  [!] {uid} 请求失败: {e}")
        return "", ""


def backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, "user_data.json.bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(USER_DATA, dst)
    print(f"[备份] {os.path.relpath(dst, SCRIPT_DIR)}")


def save_via_api(accounts):
    """走服务端合并写，绝不直接 open('w') 覆盖 user_data.json。"""
    body = json.dumps({"accounts": accounts}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + "/api/save", data=body,
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    dry = "--dry" in sys.argv
    F.init_env()
    from curl_cffi import requests as cr

    cookie = F.load_cookie()
    if not cookie.get("sessionid"):
        print("[!] 没找到有效登录 cookie（settings.json 的 sessionid 为空），无法查询")
        return 1

    targets, _ = missing_uids()
    print(f"[待补] {len(targets)} 个账号缺 sec_uid")
    if not targets:
        return 0

    found = {}
    for i, (uid, nick) in enumerate(targets, 1):
        sec, nn = query_sec_uid(cr, cookie, uid)
        flag = "✓" if sec else "×"
        print(f"  [{i}/{len(targets)}] {flag} {uid} {nick[:16]} → {sec[:24] if sec else '（空，账号可能已注销/封禁）'}")
        if sec:
            found[uid] = sec
        time.sleep(0.4)

    print(f"[结果] 查到 {len(found)}/{len(targets)}")
    if dry or not found:
        return 0

    backup()
    payload = {uid: {"sec_uid": sec} for uid, sec in found.items()}
    print("[写盘]", save_via_api(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
