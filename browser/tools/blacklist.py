#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
博主黑名单（不下载名单）公共模块。

所有抓取/export/写 DouK 参数的脚本都应先调 is_blocked() 过滤，
保证「喜欢」「收藏」里再出现这些博主时自动剔除。

黑名单文件：browser/data/黑名单_不下载.csv
字段：sec_uid, uid, nickname, reason, added_at
"""

import csv
import os
import re
import sys

# 路径统一来自 ../src/paths.py（黑名单文件在 data/ 目录）
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import paths  # noqa: E402

BLACKLIST_FILE = paths.BLACKLIST_FILE
FIELDS = ["sec_uid", "uid", "nickname", "reason", "added_at"]


def norm_name(s):
    """昵称归一化：去空白、去尾部星号/表情符号，转小写，便于模糊兜底匹配。"""
    if not s:
        return ""
    s = str(s).strip().lower()
    s = re.sub(r"[\s\*＊·•\-—_]+", "", s)
    s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]", "", s)
    return s


class Blacklist:
    def __init__(self, path=BLACKLIST_FILE):
        self.path = path
        self.sec_uids = set()
        self.uids = set()
        self.names = {}
        self.rows = []
        self._load()

    def _load(self):
        if not os.path.isfile(self.path):
            return
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                self.rows.append(r)
                if r.get("sec_uid"):
                    self.sec_uids.add(r["sec_uid"].strip())
                if r.get("uid"):
                    self.uids.add(str(r["uid"]).strip())
                n = norm_name(r.get("nickname"))
                if n:
                    self.names[n] = r.get("nickname")

    def __len__(self):
        return len(self.rows)

    def match(self, sec_uid="", uid="", nickname=""):
        """命中返回黑名单里的昵称，否则 None。"""
        if sec_uid and sec_uid.strip() in self.sec_uids:
            return self.names.get(norm_name(nickname)) or sec_uid[:16]
        if uid and str(uid).strip() in self.uids:
            return self.names.get(norm_name(nickname)) or str(uid)
        n = norm_name(nickname)
        if n and n in self.names:
            return self.names[n]
        return None

    def is_blocked(self, sec_uid="", uid="", nickname=""):
        return self.match(sec_uid, uid, nickname) is not None


_CACHE = None


def get(path=None):
    """进程内缓存，避免重复读文件。"""
    global _CACHE
    if _CACHE is None or (path and path != _CACHE.path):
        _CACHE = Blacklist(path or BLACKLIST_FILE)
    return _CACHE


def is_blocked(sec_uid="", uid="", nickname=""):
    return get().is_blocked(sec_uid, uid, nickname)


def add(sec_uid="", uid="", nickname="", reason=""):
    """追加一条（按 sec_uid 去重），返回是否新增。"""
    from datetime import datetime

    bl = get()
    if sec_uid and sec_uid in bl.sec_uids:
        return False
    new_file = not os.path.isfile(BLACKLIST_FILE)
    # 追加前确保文件以换行结尾，否则会出现「粘连行」把两条记录并成一条
    if not new_file:
        with open(BLACKLIST_FILE, "rb") as chk:
            chk.seek(0, 2)
            if chk.tell() > 0:
                chk.seek(-1, 2)
                if chk.read(1) != b"\n":
                    with open(BLACKLIST_FILE, "ab") as fix:
                        fix.write(b"\r\n")
    with open(BLACKLIST_FILE, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        w.writerow({
            "sec_uid": sec_uid, "uid": uid, "nickname": nickname,
            "reason": reason, "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    global _CACHE
    _CACHE = None          # 让下次 get() 重新加载
    return True
