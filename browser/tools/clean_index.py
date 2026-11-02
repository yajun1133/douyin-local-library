#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 data.json 里混进去的「标注字段」剥掉（不启动服务也能用）。

背景（2026-11-01 查实）：
    data.json 本该只放「扫描器产出的字段」，标注只该在 user_data.json 里。
    但 load_user_data() 是**原地 update** 进 CACHE["accounts"] 的，而
    CACHE["accounts"] 的 value 就是 CACHE["data"]["accounts"] 的同一批 dict。
    于是五个写盘点（改名单个 / 删作品 / 恢复回收站 / 批量改名 / 重复去重）
    一旦把 CACHE["data"] 整份 dump 回去，标注就跟着落了盘；而增量扫描是
    「读旧 data.json 再合并」，写进去就再也洗不掉，还会变成过期残缺值
    （实测 starred 只剩 93，真值 105）。

    server.py 已经在写盘前统一过 _index_only()，所以**新写入不会再污染**；
    这个脚本负责把**历史上已经写进去的**那批清掉。

用法:
    python clean_index.py --dry     # 只体检，不动文件
    python clean_index.py           # 清理（写前自动备份，原子替换）

安全性:
    - 只删 ANNOT_ONLY_KEYS 里那 10 个字段；works / stats / history / sec_uid /
      douyin_id / signature / nickname 一律不动
    - 改前备份到 backup/，只留最近 3 份本脚本的备份
    - 写盘走 tmp + os.replace，进程被杀也不会留下半个文件
    - 写完立刻重新读一遍做校验（账号数 / 作品数必须和改前一致）
"""

import json
import os
import sys
import time

# 代码在 ../src/：直接复用 server.py 的字段白名单和 _index_only()，
# 避免「脚本一套、服务一套」跑偏
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import paths                      # noqa: E402
import server as S                # noqa: E402

KEEP_BACKUPS = 3


def human(n):
    n = int(n or 0)
    if n >= 1024 ** 2:
        return "%.1f MB" % (n / 1024 ** 2)
    if n >= 1024:
        return "%d KB" % round(n / 1024)
    return "%d B" % n


def scan(data):
    """统计每个标注字段命中了多少个账号。"""
    accounts = data.get("accounts") or {}
    hits = {}
    for key in S.ANNOT_ONLY_KEYS:
        n = sum(1 for a in accounts.values() if isinstance(a, dict) and key in a)
        if n:
            hits[key] = n
    return hits


def works_total(data):
    return sum((a.get("stats") or {}).get("work_count", 0)
               for a in (data.get("accounts") or {}).values()
               if isinstance(a, dict))


def prune_backups():
    """只留最近 KEEP_BACKUPS 份本脚本的备份。"""
    try:
        cand = [os.path.join(paths.BACKUP_DIR, f)
                for f in os.listdir(paths.BACKUP_DIR)
                if f.startswith("data.json.清理标注前_")]
        cand.sort(key=os.path.getmtime, reverse=True)
        for p in cand[KEEP_BACKUPS:]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def main():
    dry = "--dry" in sys.argv

    if not os.path.exists(paths.DATA_JSON):
        print("[!] 找不到 data.json：%s" % paths.DATA_JSON)
        print("    先启动服务或跑一次 scanner.py 生成索引。")
        return 2

    print("data.json : %s" % paths.DATA_JSON)
    print("大小      : %s" % human(os.path.getsize(paths.DATA_JSON)))

    try:
        with open(paths.DATA_JSON, encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError) as e:
        print("[!] 读不出来，先别动它：%s" % e)
        return 2

    accounts = data.get("accounts") or {}
    hits = scan(data)
    print("账号数    : %d" % len(accounts))
    print("作品总数  : %d" % works_total(data))

    if not hits:
        print("\n[OK] 已经是干净的，data.json 里没有任何标注字段。")
        return 0

    print("\n发现标注残留：")
    for k, v in sorted(hits.items(), key=lambda x: -x[1]):
        print("  %-13s %5d 个账号" % (k, v))

    clean = S._index_only(data)
    text = json.dumps(clean, ensure_ascii=False)
    after = len(text.encode("utf-8"))
    print("\n清理后大小预计: %s（现在 %s）" % (human(after), human(os.path.getsize(paths.DATA_JSON))))

    if dry:
        print("\n[DRY] 只看不动。去掉 --dry 才会真写。")
        return 0

    # ---- 备份 ----
    paths.ensure_dirs()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = os.path.join(paths.BACKUP_DIR, "data.json.清理标注前_%s" % stamp)
    try:
        with open(paths.DATA_JSON, "rb") as src, open(bak, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)
        print("已备份    : %s" % bak)
    except OSError as e:
        print("[!] 备份失败，为了安全不往下走了：%s" % e)
        return 2

    # ---- 原子写 ----
    tmp = paths.DATA_JSON + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(text)
        os.replace(tmp, paths.DATA_JSON)
    except OSError as e:
        print("[!] 写盘失败（原文件没动）：%s" % e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 2

    # ---- 校验 ----
    try:
        with open(paths.DATA_JSON, encoding="utf-8") as fp:
            back = json.load(fp)
    except (OSError, ValueError) as e:
        print("[!] 写完读不回来，请立刻用备份恢复：%s" % bak)
        print("    %s" % e)
        return 1

    left = scan(back)
    problems = []
    if left:
        problems.append("还有标注字段：%s" % left)
    if len(back.get("accounts") or {}) != len(accounts):
        problems.append("账号数变了：%d -> %d" % (len(accounts), len(back.get("accounts") or {})))
    if works_total(back) != works_total(data):
        problems.append("作品数变了：%d -> %d" % (works_total(data), works_total(back)))

    if problems:
        print("\n[!] 校验没过：")
        for p in problems:
            print("    - %s" % p)
        print("    备份在 %s，可以整份覆盖回去。" % bak)
        return 1

    prune_backups()
    print("清理完成  : 标注字段已全部剥掉，账号数 / 作品数 / 其余字段未变")

    if S._instance_alive():
        print("\n[i] 8723 服务正在跑，它会自己重读这份新索引（标注照常从 user_data 叠加回来）。")
        print("    如果服务还是改动前的旧代码，你在网页里做一次改名/删作品就会把标注重新写回来；")
        print("    重启一次服务（stop-all.bat → start-all.bat）之后就不会了。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
