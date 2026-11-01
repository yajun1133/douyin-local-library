#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标注数据恢复工具（user_data.json：星标 / 标签 / 备注 / 状态 / 等级 / 封面 / 大小号）。

用法（在本目录下打开 cmd 执行）:
    python restore_backup.py --list                # 列出所有备份（时间 / 账号数 / 封面数）
    python restore_backup.py --latest              # 恢复到最近一份备份
    python restore_backup.py --file backup\\user_data.auto-20261027-204500.json

安全设计:
1. 恢复前，先把当前 user_data.json 另存为 backup\\user_data.before-restore-<时间>.json，
   所以「恢复」本身也可以再恢复回来，不会二次丢数据。
2. 写入走临时文件 + fsync + 原子替换，中途断电不会留半截 JSON（这是最后一道救命工具，
   以前漏了 fsync，断电时可能刚好把文件写成 0 字节）。
3. 检测到服务还在跑（127.0.0.1:8723 有响应）时会拒绝执行：服务内存里还留着旧数据，
   下次保存会把刚恢复的内容顶掉。请先关掉网页、停掉 server.py，再加 --force 强制。

恢复完重新启动服务（双击「start-browser.bat」）即可看到数据。
"""

import glob
import json
import os
import shutil
import socket
import sys
import time

# 路径统一来自 src/paths.py（代码在 src/，data/备份在上一层）
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import paths  # noqa: E402

USER_DATA = paths.USER_DATA
BACKUP_DIR = paths.BACKUP_DIR
ROOT_DIR = paths.APP_ROOT
PORT = paths.PORT


def human_time(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def summary(path):
    """读一个备份的规模，读不出来就当坏文件。"""
    try:
        with open(path, "r", encoding="utf-8") as fp:
            d = json.load(fp)
        acc = d.get("accounts") or {}
        covers = sum(1 for a in acc.values() if isinstance(a, dict) and a.get("cover"))
        return len(acc), covers, ""
    except Exception as e:
        return 0, 0, str(e)


def list_backups(with_detail=True):
    files = []
    for pat in ("user_data.auto-*.json", "user_data.json.bak-*", "user_data.before-restore-*.json"):
        files.extend(glob.glob(os.path.join(BACKUP_DIR, pat)))
    # 历史脚本（fetch_uid_by_secuid.py 等）把 .bak 写在项目根目录，这里一起收进来，
    # 否则「明明有备份却 --list 看不到」，出事时等于没备份
    files.extend(glob.glob(os.path.join(ROOT_DIR, "user_data.json.bak-*")))
    files.sort(key=os.path.getmtime, reverse=True)     # 新的在前
    if not files:
        print("没有找到任何备份。")
        return []
    print("共 %d 份备份（新的在前）：\n" % len(files))
    print("  %-3s %-20s %10s %8s %8s  %s" % ("#", "时间", "大小", "账号", "封面", "文件名"))
    for i, f in enumerate(files, 1):
        size = os.path.getsize(f) / 1024
        if with_detail:
            n, cov, err = summary(f)
            tail = ("  ⚠ " + err) if err else ""
            print("  %-3d %-20s %9.0fKB %8d %8d  %s%s"
                  % (i, human_time(os.path.getmtime(f)), size, n, cov, os.path.basename(f), tail))
        else:
            print("  %-3d %-20s %9.0fKB %8s %8s  %s"
                  % (i, human_time(os.path.getmtime(f)), size, "-", "-", os.path.basename(f)))
    return files


def server_alive():
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", PORT))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def restore(src, force=False):
    if not os.path.isfile(src):
        print("找不到备份文件：%s" % src)
        return 1
    n, cov, err = summary(src)
    if err:
        print("这个备份读不出来（%s），为安全起见不恢复。" % err)
        return 1
    if server_alive() and not force:
        print("服务正在运行（127.0.0.1:%d）。" % PORT)
        print("  请先关掉网页、停掉 server.py（或关掉那个黑色命令行窗口），再执行恢复。")
        print("  确认要强行恢复就加 --force（风险：服务内存里的旧数据下次保存会覆盖回去）。")
        return 1

    # 先把「现在」的存一份，恢复本身也可逆
    paths.ensure_dirs()
    if os.path.isfile(USER_DATA):
        keep = os.path.join(BACKUP_DIR, time.strftime("user_data.before-restore-%Y%m%d-%H%M%S.json"))
        shutil.copy2(USER_DATA, keep)
        print("当前数据已另存：%s" % os.path.basename(keep))

    tmp = USER_DATA + ".restore.tmp"
    # 复制 + fsync：os.replace 只保证目录项切换，数据没落盘时断电可能留下 0 字节文件
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        shutil.copyfileobj(fin, fout)
        fout.flush()
        os.fsync(fout.fileno())
    # 替换前再确认一次写出来的 JSON 可解析，宁可失败也不要用坏文件覆盖好数据
    if summary(tmp)[2]:
        os.remove(tmp)
        print("临时文件校验失败，已中止（原 user_data.json 未改动）。")
        return 1
    os.replace(tmp, USER_DATA)
    print("已恢复 %s（%d 个账号 / %d 个封面）"
          % (os.path.basename(src), n, cov))
    print("重新启动服务即可看到（双击「start-browser.bat」）。")
    return 0


def main():
    args = sys.argv[1:]
    if not args or "--list" in args:
        list_backups()
        print("\n恢复：python restore_backup.py --latest   或   --file backup\\xxx.json")
        return 0
    if "--latest" in args:
        files = list_backups(with_detail=False)
        if not files:
            return 1
        return restore(files[0], force="--force" in args)
    if "--file" in args:
        i = args.index("--file")
        if i + 1 >= len(args):
            print("--file 后面要跟备份文件名")
            return 1
        name = args[i + 1]
        src = name if os.path.isabs(name) or os.path.dirname(name) else os.path.join(BACKUP_DIR, name)
        return restore(src, force="--force" in args)
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
