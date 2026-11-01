#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把博主加入「不下载」黑名单。

支持三种定位方式：
  python block_author.py 亚瑟爱数学                     # 昵称（模糊匹配汇总表）
  python block_author.py https://www.douyin.com/user/MS4w...   # 主页链接
  python block_author.py MS4wLjABAAAAxxxxxxxxxxxxxxxxx...       # sec_uid
  python block_author.py 亚瑟爱数学 --reason "不想下他的作品"
  python block_author.py 亚瑟爱数学 --delete             # 加黑名单 + 删掉本地已下载文件夹

其他：
  python block_author.py --list         查看当前黑名单
  python block_author.py --remove 昵称  从黑名单移除

注意 --delete 只删磁盘文件，不改 data.json / user_data.json。
要在网页列表里彻底消失，删完再重扫一次（点顶部「🔄 重新扫描」）即可。
"""

import csv
import glob
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import blacklist as BL
import paths  # noqa: E402

OUT_DIR = paths.EXPORT_DIR


def load_all_authors():
    """从汇总 CSV 建索引：sec_uid / uid / 昵称 → 记录。"""
    idx = []
    for f in sorted(glob.glob(os.path.join(OUT_DIR, "收藏博主汇总_*.csv")),
                    key=os.path.getmtime, reverse=True):
        for r in csv.DictReader(open(f, encoding="utf-8-sig")):
            idx.append(r)
        break
    return idx


def find_target(rows, key):
    key = key.strip()
    m = re.search(r"MS4wLjAB[A-Za-z0-9_\-]+", key)
    if m:
        sec = m.group(0)
        for r in rows:
            if r.get("sec_uid") == sec:
                return r
        return {"sec_uid": sec, "uid": "", "nickname": ""}
    if key.isdigit():
        for r in rows:
            if r.get("uid") == key:
                return r
    # 昵称：先精确，再包含
    kn = BL.norm_name(key)
    exact = [r for r in rows if BL.norm_name(r.get("nickname")) == kn]
    if len(exact) == 1:
        return exact[0]
    loose = [r for r in rows if kn and kn in BL.norm_name(r.get("nickname"))]
    if len(loose) == 1:
        return loose[0]
    if len(loose) > 1:
        print(f"[?] 「{key}」匹配到 {len(loose)} 个博主，请说得更具体：")
        for r in loose[:15]:
            print(f"    {r['nickname']} | 收藏{r['count']}条 | {r['author_url']}")
        return None
    return None


def show_list():
    bl = BL.get()
    if not len(bl):
        print("黑名单为空")
        return
    print(f"黑名单共 {len(bl)} 个：\n")
    for r in bl.rows:
        print(f"  {r.get('nickname','')} | uid={r.get('uid','')} | {r.get('reason','')} | {r.get('added_at','')}")
        print(f"    {r.get('sec_uid','')}")


def main():
    args = [a for a in sys.argv[1:]]
    if not args or "--list" in args:
        show_list()
        return

    if "--remove" in args:
        i = args.index("--remove")
        key = args[i + 1] if i + 1 < len(args) else ""
        bl = BL.get()
        keep = [r for r in bl.rows
                if BL.norm_name(key) not in (BL.norm_name(r.get("nickname")),)
                and key not in (r.get("sec_uid", ""), str(r.get("uid", "")))]
        if len(keep) == len(bl.rows):
            print(f"[!] 黑名单里没找到「{key}」")
            return
        with open(BL.BLACKLIST_FILE, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=BL.FIELDS)
            w.writeheader()
            w.writerows(keep)
        print(f"[✓] 已移除「{key}」，剩余 {len(keep)} 个")
        return

    reason = ""
    if "--reason" in args:
        i = args.index("--reason")
        reason = args[i + 1] if i + 1 < len(args) else ""
        args = args[:i] + args[i + 2:]
    do_delete = "--delete" in args
    if do_delete:
        args = [a for a in args if a != "--delete"]
    key = " ".join(args).strip()
    if not key:
        print("用法: python block_author.py <昵称|主页链接|sec_uid> [--reason 说明] [--delete]")
        print("      --delete  同时删掉本地已下载的文件夹（会先列出文件数和大小，再让你确认）")
        return

    rows = load_all_authors()
    t = find_target(rows, key)
    if not t:
        print(f"[!] 找不到「{key}」")
        return

    sec, uid, nick = t.get("sec_uid", ""), t.get("uid", ""), t.get("nickname", "")
    if BL.is_blocked(sec, uid, nick):
        print(f"[=] 「{nick or key}」已经在黑名单里了")
        if not do_delete:
            return
    else:
        BL.add(sec, uid, nick, reason)
        print(f"[✓] 已加入黑名单：{nick or key}")
        print(f"    sec_uid = {sec}")
        print(f"    uid     = {uid}")
        if t.get("count"):
            print(f"    收藏数  = {t['count']}")

    if do_delete and uid:
        delete_author_folder(uid, nick)
    elif not do_delete:
        print("\n下一步：跑 apply_accounts_urls.py 把它从 DouK 下载队列里剔除")
        print("要连本地文件一起删，加 --delete")


def delete_author_folder(uid, nick=""):
    """删掉 downnnnnnnn 下这个 uid 的文件夹（含所有作品文件）。

    护栏：①只删名字以 UID<uid>_ 开头、且直接位于 SCAN_ROOT 下的目录；
          ②绝不碰 SCAN_ROOT 本身；③先把文件数/大小打出来，再删。
    只处理磁盘文件；要想从列表里彻底消失，还得让文件不再出现在 data.json
    （重扫一次即可，或按 README 的「删除博主」步骤手工清 data.json + user_data）。
    """
    root = paths.SCAN_ROOT
    if not (uid and os.path.isdir(root)):
        print("[!] 找不到扫描根目录，跳过删除")
        return
    targets = [n for n in os.listdir(root)
               if n.startswith(f"UID{uid}_") and os.path.isdir(os.path.join(root, n))]
    if not targets:
        print(f"[=] 磁盘上没有 UID{uid}_ 的文件夹（可能本来就没下载）")
        return
    for name in targets:
        p = os.path.join(root, name)
        # 护栏：必须真在 SCAN_ROOT 下一层，防止路径拼接出意外
        if os.path.normpath(os.path.dirname(os.path.normpath(p))) != os.path.normpath(root):
            print(f"[!] 路径异常，跳过：{p}")
            continue
        files = [f for f in os.listdir(p) if os.path.isfile(os.path.join(p, f))]
        size = sum(os.path.getsize(os.path.join(p, f)) for f in files)
        print(f"\n[删除] {name}")
        print(f"       文件 {len(files)} 个 / {size / 1024 / 1024:.1f} MB")
        shutil.rmtree(p)
        print(f"       ✓ 已删除（目录还在吗：{os.path.exists(p)}）")


if __name__ == "__main__":
    main()
