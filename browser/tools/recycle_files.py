#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把确认重复的多余文件移入回收站（保留原目录结构，可原样恢复）。

只在「画面实测相同」的前提下使用。调用方传入 保留文件 / 待删文件 的绝对路径。
"""
import os
import sys
import time
import uuid
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from paths import SCAN_ROOT, RECYCLE_DIR     # noqa: E402


def recycle(path):
    rel = os.path.relpath(path, SCAN_ROOT)
    dst_dir = os.path.join(RECYCLE_DIR, time.strftime("%Y%m%d-%H%M%S-") + str(uuid.uuid4().hex[:4]))
    dst = os.path.join(dst_dir, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.move(path, dst)
    return dst


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    for p in sys.argv[1:]:
        if not os.path.exists(p):
            print("跳过（不存在）", p)
            continue
        d = recycle(p)
        print("已移入回收站:", d)
