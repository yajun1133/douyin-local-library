#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从「导出」里的收藏汇总表补 sec_uid（不联网，纯本地查表）。

背景（2026-10-28）：
    扫描器给账号配 sec_uid 只有一条路 —— 读 downnnnnnnn\\Data\\*.xlsx（每个作者一个）。
    但新下载的博主常常**没有生成 xlsx**，扫完 sec_uid 全空：
    抖音按钮点不开主页、大小号识别也认不出他们。

    而 export\\收藏博主汇总_<时间>.csv 里本来就有全部收藏博主的
    uid / sec_uid / 昵称（7353 条），按 UID 精确匹配就能补齐，不用联网反查。

    只有在导出表里也找不到的，才去跑 legacy\\fill_missing_secuid.py 联网查。

用法（服务开着就行，脚本走 /api/save 合并写，不会覆盖标注）:
    python tools\\fill_secuid_from_export.py --dry      # 只看能补几个，不写
    python tools\\fill_secuid_from_export.py            # 补齐（先自动备份 user_data）
    python tools\\fill_secuid_from_export.py --file "export\\某个.csv"

    --source 可指定额外的对照表（默认自动扫 export\\*.csv 和 data\\uid_secuid_对照表.csv）

安全规则：
    1. 只写 sec_uid 一个字段，已有值的一律不覆盖。
    2. 走服务端 /api/save → merge_save_user_data()（合并写 + 原子替换），
       绝不用 open("w") 整份覆盖 user_data.json。
    3. 写前自动备份到 backup\\user_data.before-secuid-<时间>.json。
"""

import csv
import json
import os
import re
import shutil
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import paths  # noqa: E402

API = "http://127.0.0.1:8723"
UID_FOLDER_RE = re.compile(r"^UID(\d+)_")


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


def read_ref_tables(extra_file=None):
    """把 export/*.csv 和 data/uid_secuid_对照表.csv 都读进来，合成 uid -> (sec_uid, 昵称)。

    优先级：后读到的覆盖先读到的，所以把「收藏博主汇总」放最后（它最全、最新）。
    """
    ref = {}
    files = []

    # 1) 手工维护的对照表（老数据，作为兜底）
    p = os.path.join(paths.DATA_DIR, "uid_secuid_对照表.csv")
    if os.path.isfile(p):
        files.append(p)

    # 2) 导出目录里的所有 csv（按修改时间从旧到新，新的覆盖旧的）
    if os.path.isdir(paths.EXPORT_DIR):
        csvs = [os.path.join(paths.EXPORT_DIR, f)
                for f in os.listdir(paths.EXPORT_DIR) if f.lower().endswith(".csv")]
        csvs.sort(key=lambda f: os.path.getmtime(f))
        files.extend(csvs)

    if extra_file:
        files.append(extra_file)

    for f in files:
        got = 0
        try:
            with open(f, "r", encoding="utf-8-sig", errors="replace", newline="") as fp:
                rd = csv.DictReader(fp)
                if not rd.fieldnames:
                    continue
                # 列名归一化：uid / sec_uid / 昵称
                norm = {(h or "").strip().lower(): h for h in rd.fieldnames}
                c_uid = norm.get("uid")
                c_sec = norm.get("sec_uid") or norm.get("secuid")
                if not c_uid or not c_sec:
                    continue
                c_nick = norm.get("nickname") or norm.get("昵称") or None
                for row in rd:
                    u = (row.get(c_uid) or "").strip()
                    s = (row.get(c_sec) or "").strip()
                    if not u or not s:
                        continue
                    # 从主页链接兜底（列名可能是 author_url / 主页链接）
                    if not s.startswith("MS4w"):
                        for k, v in row.items():
                            if v and "douyin.com/user/" in str(v):
                                s = str(v).split("/user/")[-1].split("?")[0].strip()
                                break
                    if not s.startswith("MS4w"):
                        continue
                    ref[u] = (s, (row.get(c_nick) or "").strip() if c_nick else "")
                    got += 1
        except (OSError, csv.Error) as e:
            print("  [!] 读取失败 %s: %s" % (os.path.basename(f), e))
            continue
        if got:
            print("  + %-46s %5d 条" % (os.path.basename(f)[:46], got))
    return ref


def scan_new_uids():
    """扫 downnnnnnnn 下的 UID 文件夹，返回 {uid: 文件夹名}（只关心 uid）。"""
    out = {}
    if not os.path.isdir(paths.SCAN_ROOT):
        return out
    for name in os.listdir(paths.SCAN_ROOT):
        m = UID_FOLDER_RE.match(name)
        if m and os.path.isdir(os.path.join(paths.SCAN_ROOT, name)):
            out[m.group(1)] = name
    return out


def backup():
    os.makedirs(paths.BACKUP_DIR, exist_ok=True)
    dst = os.path.join(paths.BACKUP_DIR,
                       "user_data.before-secuid-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(paths.USER_DATA, dst)
    print("[备份] %s" % os.path.relpath(dst, paths.APP_ROOT))


def save_via_api(accounts):
    body = json.dumps({"accounts": accounts}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(API + "/api/save", data=body,
                                headers={"Content-Type": "application/json"},
                                method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def main():
    dry = "--dry" in sys.argv
    extra = None
    if "--file" in sys.argv:
        i = sys.argv.index("--file")
        if i + 1 < len(sys.argv):
            extra = sys.argv[i + 1]

    print("[1/3] 读取对照表...")
    ref = read_ref_tables(extra)
    print("      合计 uid 条数: %d" % len(ref))
    if not ref:
        print("[!] 没有任何可用对照表（export\\*.csv 里要有 uid + sec_uid 两列）")
        return 1

    print("[2/3] 比对本地账号...")
    dacc = load_json(paths.DATA_JSON).get("accounts") or {}
    uacc = load_json(paths.USER_DATA).get("accounts") or {}
    folders = scan_new_uids()

    # data.json 里没有、但文件夹在的（扫描没跑或跑一半）→ 提示，不写
    orphan = [u for u in folders if u not in dacc]
    if orphan:
        print("      [!] %d 个文件夹还没进 data.json（先跑 src\\scanner.py 再补）" % len(orphan))

    todo, already = {}, 0
    for uid, a in dacc.items():
        cur = (a.get("sec_uid") or "").strip() or ((uacc.get(uid) or {}).get("sec_uid") or "").strip()
        if cur:
            already += 1
            continue
        hit = ref.get(uid)
        if hit:
            todo[uid] = hit[0]

    print("      已有 sec_uid: %d | 缺失: %d | 本次能补: %d"
          % (already, len(dacc) - already, len(todo)))
    still = [u for u, a in dacc.items()
             if not ((a.get("sec_uid") or "").strip()
                     or ((uacc.get(u) or {}).get("sec_uid") or "").strip())
             and u not in todo]
    if still:
        print("      表里也查不到: %d 个" % len(still))
        for u in still[:12]:
            print("        %s  %s" % (u, (dacc[u].get("nickname") or "")[:18]))
        if len(still) > 12:
            print("        ...")
        print("      → 这些再跑: \"C:\\Python312\\python.exe\" tools\\legacy\\fill_missing_secuid.py")

    if not todo:
        print("\n[完成] 没有需要补的账号。")
        return 0
    if dry:
        print("\n[dry] 不写盘。去掉 --dry 才会写入。")
        for u, s in list(todo.items())[:8]:
            print("        %s → %s..." % (u, s[:26]))
        return 0

    print("\n[3/3] 写入（只写 sec_uid，已有值不覆盖）...")
    backup()
    payload = {uid: {"sec_uid": sec} for uid, sec in todo.items()}
    print("[结果]", save_via_api(payload))

    # 复查
    uacc2 = load_json(paths.USER_DATA).get("accounts") or {}
    ok = sum(1 for uid in todo if (uacc2.get(uid) or {}).get("sec_uid"))
    print("[复查] 实际写入 %d/%d 条" % (ok, len(todo)))
    print("[提示] 浏览器点顶部「⟳ 刷新」即可看到新数据（不用重启服务）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
