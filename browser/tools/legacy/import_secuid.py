#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从本机已有的资料里，把 UID 对应的 SEC_UID 等补进 user_data.json（标注文件，重扫不丢）。

数据源（有哪个用哪个，自动跳过缺失的）：
1. ../抖音下载数据记录.md        作者表：UID | 昵称 | SEC_UID | 主页简介
2. ../.../docs/抖音11111.xlsx    追踪表：昵称 | 大小号 | sec_uid(链接) | 标签
3. ../.../docs/抖音临时.xlsx      追踪表：昵称 | [大小号] | sec_uid(链接)

规则（宁缺毋滥）：
- 只补空缺，不覆盖已有 SEC_UID
- xlsx 按**昵称精确匹配 UID**，一个昵称只对应唯一账号时才采用；重名跳过并计数
- 大小号：追踪表里「大小号」列同前缀（如 丫丫1/丫丫2/丫丫3、喝多了想兔1/2/3）视为同一组，
  组内互相写入 alt_accounts（双向）

用法：
    python import_secuid.py --dry     # 只看统计，不写盘
    python import_secuid.py           # 写盘
"""

import json
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
import sys  # noqa: E402
sys.path.insert(0, _TOOLS)                       # tools/ 下的 blacklist、downloaded
sys.path.insert(0, os.path.join(_ROOT, "src"))   # src/paths.py 统一路径
import paths  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # 仅用于拼本目录内的东西
DATA_JSON = os.path.join(SCRIPT_DIR, "data.json")
USER_DATA = paths.USER_DATA
MD_PATH = os.path.join(os.path.dirname(paths.APP_ROOT), "_archive", "抖音下载数据记录.md")
DOC_DIR = r"E:\dyyyyyyyyyyyyydownload\TikTokDownloader_V5.6_Windows_X64\文档"
TRACK_FILES = ["抖音11111.xlsx", "抖音临时.xlsx"]

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
URL_SEC_RE = re.compile(r"(MS4wLjAB[A-Za-z0-9_\-]+)")
SEC_RE = re.compile(r"^MS4wLjAB[A-Za-z0-9_\-]+$")


# ---------- 解析 md 作者表 ----------
def parse_md(path):
    table = {}
    if not os.path.isfile(path):
        return table
    row_re = re.compile(r"^\|\s*\d+\s*\|\s*(\d+)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*([^|]*?)\s*\|\s*$")
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            m = row_re.match(line.strip())
            if not m:
                continue
            uid, nick, sec, sign = m.group(1), m.group(2), m.group(3), m.group(4)
            it = table.setdefault(uid, {"nickname": "", "sec_uid": "", "signature": ""})
            it["nickname"] = it["nickname"] or nick
            it["sec_uid"] = it["sec_uid"] or (sec if SEC_RE.match(sec) else "")
            it["signature"] = it["signature"] or sign
    return table


# ---------- 解析 xlsx（昵称 / 大小号 / 链接） ----------
def read_xlsx_rows(path):
    if not os.path.isfile(path):
        return []
    try:
        z = zipfile.ZipFile(path)
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            sst = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(NS + "t")) for si in sst.findall(NS + "si")]
        sheet = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    except Exception as e:
        print(f"[!] 读取失败 {path}: {e}")
        return []
    out = []
    for row in sheet.findall(f".//{NS}row"):
        vals = []
        for c in row.findall(f"{NS}c"):
            v = c.find(f"{NS}v")
            t = c.get("t")
            if t == "s" and v is not None:
                vals.append(shared[int(v.text)] if int(v.text) < len(shared) else "")
            elif t == "inlineStr":
                ise = c.find(f"{NS}is")
                vals.append("".join(x.text or "" for x in ise.iter(NS + "t")) if ise is not None else "")
            else:
                vals.append(v.text if v is not None else "")
        out.append([x.strip() for x in vals])
    return out


def parse_tracker(path):
    """返回 [(nickname, group_label, sec_uid)]"""
    rows = read_xlsx_rows(path)
    out = []
    for r in rows:
        if not r or not r[0] or r[0] == "昵称":
            continue
        nick = r[0].strip()
        cells = r[1:]
        sec = ""
        group = ""
        for cell in cells:
            if not cell:
                continue
            m = URL_SEC_RE.search(cell)
            if m and not sec:
                sec = m.group(1)
                continue
            if cell.startswith("http"):
                continue
            if not group and len(cell) <= 20:
                group = cell
        if nick:
            out.append((nick, group, sec))
    return out


def group_prefix(label):
    """丫丫2直播 → 丫丫；喝多了想兔3 → 喝多了想兔；无数字后缀则用原值"""
    s = re.sub(r"(直播|小号|大号|主号)+$", "", label or "")
    s = re.sub(r"\d+$", "", s)
    return s.strip()


def main():
    dry = "--dry" in sys.argv
    with open(DATA_JSON, "r", encoding="utf-8") as fp:
        accounts = json.load(fp).get("accounts", {})

    user_data = {"accounts": {}}
    if os.path.isfile(USER_DATA):
        try:
            with open(USER_DATA, "r", encoding="utf-8") as fp:
                user_data = json.load(fp) or {"accounts": {}}
        except ValueError:
            print("[!] user_data.json 解析失败，按空处理")
    uacc = user_data.setdefault("accounts", {})

    def cur(uid, field):
        return (uacc.get(uid, {}) or {}).get(field) or accounts.get(uid, {}).get(field) or ""

    def put(uid, field, value):
        uacc.setdefault(uid, {})[field] = value

    # 昵称 -> [uid]，只认唯一昵称
    by_nick = defaultdict(list)
    for uid, a in accounts.items():
        n = (a.get("nickname") or "").strip()
        if n:
            by_nick[n].append(uid)

    # ---------- 1) md：UID 直接对应 ----------
    md = parse_md(MD_PATH)
    md_sec = md_sign = 0
    for uid, row in md.items():
        if uid not in accounts:
            continue
        if row["sec_uid"] and not cur(uid, "sec_uid"):
            put(uid, "sec_uid", row["sec_uid"]); md_sec += 1
        if row["signature"] and not cur(uid, "signature"):
            put(uid, "signature", row["signature"]); md_sign += 1
    print(f"[md] 作者行 {len(md)}   补 sec_uid {md_sec}   补简介 {md_sign}")

    # ---------- 2) xlsx 追踪表：昵称 -> uid ----------
    tracker = []
    for f in TRACK_FILES:
        rows = parse_tracker(os.path.join(DOC_DIR, f))
        print(f"[xlsx] {f}: {len(rows)} 行")
        tracker.extend(rows)

    x_sec = 0
    amb = 0
    nogroup = 0
    groups = defaultdict(list)     # 前缀 -> [(uid, label)]
    for nick, group, sec in tracker:
        uids = by_nick.get(nick, [])
        if not uids:
            continue
        if len(uids) > 1:
            # 重名：只有当其中恰好一个缺 sec_uid 时才敢填
            empty = [u for u in uids if not cur(u, "sec_uid")]
            if sec and len(empty) == 1:
                put(empty[0], "sec_uid", sec); x_sec += 1
                uids = empty
            else:
                amb += 1
                continue
        uid = uids[0]
        if sec and not cur(uid, "sec_uid"):
            put(uid, "sec_uid", sec); x_sec += 1
        if group:
            pre = group_prefix(group)
            if pre:
                groups[pre].append((uid, group))
    print(f"[xlsx] 补 sec_uid {x_sec}   重名跳过 {amb}")

    # ---------- 3) 大小号分组 → 双向 alt_accounts ----------
    links = 0
    group_count = 0
    for pre, members in groups.items():
        uids = sorted({u for u, _ in members})
        if len(uids) < 2:
            nogroup += 1
            continue
        group_count += 1
        for a in uids:
            for b in uids:
                if a == b:
                    continue
                arr = list((uacc.get(a, {}) or {}).get("alt_accounts") or [])
                if b not in arr:
                    arr.append(b)
                    put(a, "alt_accounts", arr)
                    links += 1
    print(f"[大小号] 成组 {group_count} 组，写入关联 {links} 条（双向）")

    # 写盘前归一化：alt_accounts 必须是「组内全连」，否则加上已有组会变散关系
    try:
        from fix_alt_groups import normalize as normalize_alts
        fixed = normalize_alts(uacc)
        if fixed:
            print(f"[大小号] 归一化为组内全连：修正 {fixed} 条")
    except Exception as e:
        print(f"[!] 归一化跳过：{e}")

    total_sec = sum(1 for uid in accounts if cur(uid, "sec_uid"))
    print(f"[合计] 有 SEC_UID 的账号：{total_sec} / {len(accounts)}")

    if dry:
        print("（--dry：未写盘）")
        return
    # 写盘前备份 + 原子替换，防止把现有标注覆盖坏（对齐 fix_alt_groups 的做法）
    import shutil, tempfile, os as _os
    from datetime import datetime as _dt
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    _bak_dir = paths.BACKUP_DIR
    _os.makedirs(_bak_dir, exist_ok=True)
    backup = _os.path.join(_bak_dir, _os.path.basename(USER_DATA) + ".bak-" + stamp)
    shutil.copy2(USER_DATA, backup)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(USER_DATA), prefix=".user_data.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
            json.dump(user_data, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, USER_DATA)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    print(f"已写入 {USER_DATA}（备份 {os.path.basename(backup)}）")


if __name__ == "__main__":
    main()
