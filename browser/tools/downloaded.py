#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「已下载博主」公共模块 —— 以 DouK 的下载目录为唯一权威依据。

为什么不用 data.json / user_data.json：
    那两个文件是旧索引，实测滞后严重（下载目录有 1666 个，data.json 里只认出十几个），
    会导致已经下过的博主被重新排进队列、重复下载。

文件夹命名规则（DouK）：  UID{uid}_{昵称}_{发布作品|喜欢作品|收藏作品|合集}
注意：DouK 会把昵称里的 emoji 去掉，所以昵称只能做模糊兜底，**uid 才是精确键**。

用法：
    import downloaded as DL
    DL.is_downloaded(uid="123", nickname="某某")      # True/False
    DL.get().uids                                     # 已下载 uid 集合
    python downloaded.py                              # 生成 export/已下载账号索引.csv
"""

import csv
import os
import re
import sys

# 路径统一来自 ../src/paths.py（导出目录在项目根目录的 export/）
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import paths  # noqa: E402

OUT_DIR = paths.EXPORT_DIR

DOWNLOAD_ROOT = r"E:\dyyyyyyyyyyyyydownload\TikTokDownloader_V5.6_Windows_X64\downnnnnnnn"
_FOLDER_RE = re.compile(
    r"^(UID|CID)(\d+)_(.*?)_(发布作品|喜欢作品|收藏作品|收藏夹作品|合集|作品)$")


def norm_name(s):
    """归一化昵称：去空白/标点/emoji，便于和文件夹名比对（DouK 会剥掉 emoji）。"""
    if not s:
        return ""
    s = str(s).strip().lower()
    s = re.sub(r"[\s\*＊·•\-—_\.。>_<＞＜（）()\[\]【】]+", "", s)
    s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2190-\u21FF]", "", s)
    return s


class DownloadedIndex:
    def __init__(self, root=DOWNLOAD_ROOT, count_files=False):
        self.root = root
        self.uids = set()          # 已下载 uid
        self.cids = set()          # 收藏夹 id
        self.names = {}            # 归一化昵称 -> 文件夹里的原昵称
        self.records = []          # 明细
        self.count_files = count_files
        self._load()

    def _load(self):
        if not os.path.isdir(self.root):
            return
        for entry in os.scandir(self.root):
            if not entry.is_dir():
                continue
            m = _FOLDER_RE.match(entry.name)
            if not m:
                continue
            prefix, uid, name, kind = m.group(1), m.group(2), m.group(3), m.group(4)
            (self.uids if prefix == "UID" else self.cids).add(uid)
            n = norm_name(name)
            if n:
                self.names.setdefault(n, name)
            self.records.append({
                "uid": uid if prefix == "UID" else "",
                "folder_nickname": name,
                "kind": kind,
                "folder": entry.name,
                "files": _count_files(entry.path) if self.count_files else -1,
            })

    def match(self, uid="", nickname=""):
        """命中返回命中依据，否则 None。

        原则：**给了 uid 就只认 uid**。昵称兜底只用于没 uid 的场景，
        否则「Xx」「小宅」「小怡」这种重名昵称会把别人误判成已下载。
        """
        if uid:
            return str(uid).strip() if str(uid).strip() in self.uids else None
        n = norm_name(nickname)
        if n and len(n) >= 2 and n in self.names:
            return self.names[n]
        return None

    def is_downloaded(self, uid="", nickname=""):
        return self.match(uid, nickname) is not None

    def __len__(self):
        return len(self.uids)


def _count_files(path):
    try:
        return sum(1 for e in os.scandir(path) if e.is_file())
    except OSError:
        return 0


_CACHE = None


def get(root=None):
    global _CACHE
    if _CACHE is None or (root and root != _CACHE.root):
        _CACHE = DownloadedIndex(root or DOWNLOAD_ROOT)
    return _CACHE


def is_downloaded(uid="", nickname=""):
    return get().is_downloaded(uid, nickname)


def main():
    want_files = "--files" in sys.argv
    idx = DownloadedIndex(count_files=want_files)
    global _CACHE
    _CACHE = idx
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "已下载账号索引.csv")
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["uid", "folder_nickname", "kind", "files", "folder"])
        w.writeheader()
        rows = sorted(idx.records, key=lambda r: (r["kind"], r["folder_nickname"]))
        w.writerows(rows)
    all_dirs = [d for d in os.listdir(idx.root) if os.path.isdir(os.path.join(idx.root, d))]
    print(f"[索引] 下载目录 {idx.root}")
    print(f"[索引] 目录 {len(all_dirs)} 个 · 识别人物 {len(idx.uids)} 个 · "
          f"收藏夹 {len(idx.cids)} 个 · 命名不符 {len(all_dirs) - len(idx.records)} 个")
    if not want_files:
        print("[提示] files 列未统计（加 --files 会慢约 1.5 分钟）")
    print(f"[输出] {out}")
    return out


if __name__ == "__main__":
    main()
