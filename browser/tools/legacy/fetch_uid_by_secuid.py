#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按 sec_uid 从抖音批量获取 UID 与账号资料，生成对照表并回填。

复用 DouK-Downloader 的 curl_cffi（浏览器指纹）+ settings.json 里的登录 cookie，
调抖音 aweme/v1/web/im/user/info/ 接口（支持批量 sec_user_ids），一次拿多个账号的
uid / sec_uid / 昵称 / 抖音号。

为什么不用昵称匹配：昵称、抖音号都会改，只有 uid 和 sec_uid 稳定。这里直接拿权威
uid，再按 uid 精确回填，不依赖任何可变的昵称。

用法：
    python fetch_uid_by_secuid.py --dry     # 只查 1 个，验证链路
    python fetch_uid_by_secuid.py           # 全量查询，生成对照表 CSV + 回填 user_data.json
    python fetch_uid_by_secuid.py --no-fill # 只生成对照表，不回填

依赖（全部来自本机下载器，不联网安装）：
    DouK-Downloader_V5.8_Windows_X64/lib/           curl_cffi + 依赖 DLL
    DouK-Downloader_V5.8_Windows_X64/Volume/settings.json  登录 cookie
"""

import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
import csv
import time

DOWNLOADER_DIR = r"E:\dyyyyyyyyyyyyydownload\TikTokDownloader_V5.6_Windows_X64\DouK-Downloader_V5.8_Windows_X64"
LIB_DIR = os.path.join(DOWNLOADER_DIR, "lib")
SETTINGS = os.path.join(DOWNLOADER_DIR, "Volume", "settings.json")
DOC_DIR = r"E:\dyyyyyyyyyyyyydownload\TikTokDownloader_V5.6_Windows_X64\文档"
TRACK_FILES = ["抖音11111.xlsx", "抖音临时.xlsx"]

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
import sys  # noqa: E402
sys.path.insert(0, _TOOLS)                       # tools/ 下的 blacklist、downloaded
sys.path.insert(0, os.path.join(_ROOT, "src"))   # src/paths.py 统一路径
import paths  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # 仅用于拼本目录内的东西
DATA_JSON = os.path.join(SCRIPT_DIR, "data.json")
USER_DATA = paths.USER_DATA
OUT_TABLE = os.path.join(SCRIPT_DIR, "uid_secuid_对照表.csv")

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
SEC_RE = re.compile(r"(MS4wLjAB[A-Za-z0-9_\-]+)")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def init_env():
    os.add_dll_directory(os.path.abspath(LIB_DIR))
    os.add_dll_directory(os.path.abspath(os.path.join(LIB_DIR, "curl_cffi.libs")))
    if LIB_DIR not in sys.path:
        sys.path.insert(0, os.path.abspath(LIB_DIR))


def load_cookie():
    if not os.path.isfile(SETTINGS):
        return {}
    try:
        s = json.load(open(SETTINGS, encoding="utf-8-sig"))
        return s.get("cookie") or {}
    except Exception:
        return {}


def read_xlsx_rows(path):
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iter(NS + "t")) for si in sst.findall(NS + "si")]
    rows = []
    for sn in [n for n in z.namelist() if n.startswith("xl/worksheets/") and n.endswith(".xml")]:
        sheet = ET.fromstring(z.read(sn))
        for row in sheet.findall(f".//{NS}row"):
            vals = []
            for c in row.findall(f"{NS}c"):
                v = c.find(f"{NS}v"); t = c.get("t")
                if t == "s" and v is not None:
                    vals.append(shared[int(v.text)] if int(v.text) < len(shared) else "")
                elif t == "inlineStr":
                    ise = c.find(f"{NS}is")
                    vals.append("".join(x.text or "" for x in ise.iter(NS + "t")) if ise is not None else "")
                else:
                    vals.append(v.text if v is not None else "")
            rows.append([x.strip() for x in vals])
    return rows


def load_sec_uids():
    """从追踪表 xlsx 提取：sec_uid -> {nickname, alt(备注名/大小号)}。"""
    sec_map = {}   # sec_uid -> {'nickname', 'alt'}
    for f in TRACK_FILES:
        path = os.path.join(DOC_DIR, f)
        if not os.path.isfile(path):
            continue
        for r in read_xlsx_rows(path):
            if not r or not r[0] or r[0] == "昵称":
                continue
            nick = r[0]
            sec = ""
            alt = ""
            for cell in r[1:]:
                m = SEC_RE.search(cell)
                if m and not sec:
                    sec = m.group(1)
                elif cell and not alt and not cell.startswith("http") and len(cell) <= 20 and "MS4w" not in cell:
                    alt = cell
            if sec:
                sec_map.setdefault(sec, {"nickname": nick, "alt": alt})
    return sec_map


def query_batch(cr, cookie, sec_list):
    """一次查多个 sec_uid，返回 {sec_uid: {uid, nickname, unique_id, short_id}}。"""
    if not sec_list:
        return {}
    body = {"sec_user_ids": json.dumps(sec_list)}
    params = {
        "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
        "version_code": "170400", "version_name": "17.4.0",
    }
    resp = cr.post("https://www.douyin.com/aweme/v1/web/im/user/info/",
                   params=params, data=body,
                   headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"},
                   cookies=cookie, impersonate="chrome131", timeout=30)
    d = json.loads(resp.text)
    out = {}
    for u in (d.get("data") or []):
        sec = u.get("sec_uid")
        if sec:
            out[sec] = {
                "uid": u.get("uid"),
                "nickname": u.get("nickname"),
                "unique_id": u.get("unique_id"),
                "short_id": u.get("short_id"),
            }
    return out


def main():
    dry = "--dry" in sys.argv
    no_fill = "--no-fill" in sys.argv
    init_env()
    from curl_cffi import requests as cr

    cookie = load_cookie()
    if not cookie.get("sessionid"):
        print("[!] 未找到有效登录 cookie（settings.json 里 sessionid 为空），无法查询")
        return
    print(f"[cookie] sessionid = {cookie.get('sessionid', '')[:20]}...")

    sec_map = load_sec_uids()
    print(f"[xlsx] 提取到 {len(sec_map)} 个唯一 sec_uid")

    if dry:
        sec_list = list(sec_map.keys())[:3]
        result = query_batch(cr, cookie, sec_list)
        print("\n=== 单测结果 ===")
        for sec, info in result.items():
            print(f"  uid={info['uid']} | 昵称={info['nickname']} | 抖音号={info['unique_id']} | sec={sec[:20]}...")
        return

    # 全量查询（批量，每次 20 个）
    sec_list = list(sec_map.keys())
    all_result = {}
    BATCH = 20
    done = 0
    print(f"[task] 开始批量查询 {len(sec_list)} 个 sec_uid（每批 {BATCH} 个）...")
    for i in range(0, len(sec_list), BATCH):
        chunk = sec_list[i:i + BATCH]
        try:
            r = query_batch(cr, cookie, chunk)
            all_result.update(r)
            done += len(chunk)
            print(f"  进度 {done}/{len(sec_list)} · 已获取 {len(all_result)} 个 uid")
        except Exception as e:
            print(f"  [!] 批次 {i} 失败: {e}")
        time.sleep(0.3)

    print(f"\n[结果] 成功获取 {len(all_result)} / {len(sec_list)} 个 uid")

    # 写对照表 CSV
    with open(OUT_TABLE, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["uid", "sec_uid", "昵称(权威)", "抖音号", "备注名(大小号)", "来源昵称"])
        for sec, src in sec_map.items():
            info = all_result.get(sec)
            if info:
                w.writerow([info["uid"], sec, info["nickname"], info["unique_id"], src["alt"], src["nickname"]])
            else:
                w.writerow(["", sec, "", "", src["alt"], src["nickname"]])
    print(f"[表] 对照表已写 {OUT_TABLE}")

    if no_fill:
        print("（--no-fill：未回填 user_data.json）")
        return

    # 回填：按 uid 精确匹配 data.json 的账号 key
    data = json.load(open(DATA_JSON, encoding="utf-8"))
    accounts = data.get("accounts", {})

    ud = {"accounts": {}}
    if os.path.isfile(USER_DATA):
        try:
            ud = json.load(open(USER_DATA, encoding="utf-8")) or {"accounts": {}}
        except ValueError:
            ud = {"accounts": {}}
    uacc = ud.setdefault("accounts", {})

    filled = 0
    not_found = 0
    for sec, info in all_result.items():
        uid = str(info["uid"])
        if uid in accounts:
            if not accounts[uid].get("sec_uid") and not (uacc.get(uid, {}) or {}).get("sec_uid"):
                uacc.setdefault(uid, {})["sec_uid"] = sec
                filled += 1
        else:
            not_found += 1

    # 原子写（备份 + 临时文件替换）
    import tempfile, shutil
    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = USER_DATA + ".bak-" + stamp
    shutil.copy2(USER_DATA, backup)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=SCRIPT_DIR, prefix=".user_data.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
            json.dump(ud, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, USER_DATA)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    print(f"\n[回填] 补入 {filled} 个 sec_uid（uid 不在 data.json 的 {not_found} 个，可能是新账号）")
    print(f"[备份] {os.path.basename(backup)}")


if __name__ == "__main__":
    main()
