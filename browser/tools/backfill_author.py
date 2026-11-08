#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按作者做一次「全量对齐」：拉主页全部作品，本地缺的补下。

和追更的 `scan_round` 的区别：追更有**基线**（只认加入时间之后的新作品），
历史漏下的永远补不到。这个工具不看基线，直接用库索引 (date, 描述) 判重，
所以能把老作品补齐，也不会重复下载。

判重口径与追更第 3 层完全一致（含 `#...` 省略号的前后缀模糊匹配），
见 `watch_core.is_known`。

另外一条铁律：**DouK 下载记录里有、本地却没有的作品，视为用户删掉了，不补**。
（用户删作品就是为了不要它；补下载工具替他「恢复」等于跟用户对着干。
 DouK 的 `download_data` 是「下过又没了」的唯一凭据 —— 文件删掉后本地没别的痕迹。）

用法：
    # 先停追更常驻，避免两边同时打接口（面板上点停止，或 POST /api/watch stop）
    C:\\Python312\\python.exe tools\\backfill_author.py <uid>
    C:\\Python312\\python.exe tools\\backfill_author.py <uid1> <uid2> --pages 12

sec_uid 的取法（按顺序）：
    1. `--sec-uid` 显式传入（多个 uid 时用 `uid:sec_uid` 形式）
    2. data/watch.json 里该作者的条目
    3. data/user_data.json 里该作者的条目
三处都没有就跳过并提示，不会瞎猜。
"""
import argparse
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import paths          # noqa: E402
import watch_core as W  # noqa: E402


def _read_json(p, default):
    try:
        with open(p, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return default


def resolve_authors(uids, extra_sec=None):
    """{uid: {sec_uid, nickname}}，取不到的 sec_uid 留空由调用方报错。"""
    watch = _read_json(os.path.join(paths.DATA_DIR, "watch.json"), {})
    udata = _read_json(os.path.join(paths.DATA_DIR, "user_data.json"), {})
    wmap = {str(a.get("uid") or ""): a for a in (watch.get("authors") or [])}
    umap = udata.get("accounts") if isinstance(udata, dict) and "accounts" in udata else udata
    if not isinstance(umap, dict):
        umap = {}

    out = {}
    for uid in uids:
        sec = (extra_sec or {}).get(uid, "")
        nick = ""
        for src in (wmap.get(uid), umap.get(uid)):
            if not isinstance(src, dict):
                continue
            sec = sec or str(src.get("sec_uid") or "")
            nick = nick or str(src.get("nickname") or "")
        out[uid] = {"sec_uid": sec, "nickname": nick or uid}
    return out


def backfill_one(dy, lib_idx, lib_folders, uid, sec, nick, pages, douk_ids=None):
    """跑一位作者，返回 (新增, 跳过, 失败, 已删)。"""
    douk_ids = douk_ids or set()
    try:
        al = dy.author_works(sec, pages=pages)
    except Exception as e:
        print("× 查询失败：%s: %s" % (type(e).__name__, e))
        return 0, 0, 1, 0

    al.sort(key=lambda a: int(a.get("create_time") or 0))
    folder = lib_folders.get(uid) or ""
    author_dir = os.path.join(paths.SCAN_ROOT, folder) if folder else ""
    if not author_dir:
        print("× 库里没有这位作者的目录，先跑一次扫描再来")
        return 0, 0, 1, 0

    local = lib_idx.get(uid) or set()
    date_idx = W.build_date_index(local)
    print("接口返回 %d 条 | 本地索引 %d 条 | 目录 %s" % (len(al), len(local), folder))

    dl = sk = fail = gone = 0
    misses = []
    for a in al:
        ct = int(a.get("create_time") or 0)
        date = datetime.fromtimestamp(ct).strftime("%Y-%m-%d")
        desc = a.get("desc") or ""
        if W.is_known(local, date_idx, date, desc, 64):
            sk += 1
            continue
        # DouK 记录里有、本地却没有 = 用户自己删掉的。**别再补回来**：
        # 删作品就是为了不要它，补下载工具不该跟用户对着干。
        if str(a.get("aweme_id") or "") in douk_ids:
            gone += 1
            print("  - %s  %s  （DouK 下过、本地没有 → 当你删了，跳过）"
                  % (date, desc[:40]))
            continue
        misses.append(date)
        files, size, err = W.download_aweme(dy, a, author_dir, nick, 64)
        if files:
            dl += 1
            k = W.key_of_desc(desc, 64)
            local.add((date, k))
            date_idx.setdefault(date, []).append(k)
            print("  ↓ %s  %s  (%.1f MB)" % (date, desc[:44], size / 1048576.0))
        elif err:
            fail += 1
            print("  ✗ %s  %s  %s" % (date, desc[:30], err[:90]))
        else:
            sk += 1
    print("-" * 64)
    print("汇总：新增 %d  跳过 %d  失败 %d  已删不补 %d  (接口总数 %d)"
          % (dl, sk, fail, gone, len(al)))
    if misses:
        print("  本地原本缺失的日期：%s" % ", ".join(sorted(set(misses))))
    return dl, sk, fail, gone


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="按作者全量对齐（补下本地缺的作品）")
    ap.add_argument("uids", nargs="+", help="作者 uid，可多个")
    ap.add_argument("--pages", type=int, default=12,
                    help="每个 uid 最多翻几页（每页约 20 条，默认 12 页≈240 条）")
    ap.add_argument("--sec-uid", action="append", default=[],
                    metavar="UID:SEC_UID",
                    help="显式指定 sec_uid，库里查不到时用")
    args = ap.parse_args()

    extra = {}
    for s in args.sec_uid:
        if ":" in s:
            k, v = s.split(":", 1)
            extra[k.strip()] = v.strip()

    authors = resolve_authors([str(u) for u in args.uids], extra)
    missing = [u for u, a in authors.items() if not a["sec_uid"]]
    if missing:
        print("× 这些 uid 查不到 sec_uid，请用 --sec-uid UID:SEC_UID 补上：%s"
              % ", ".join(missing))
        for u in missing:
            authors.pop(u)
    if not authors:
        return 2

    dy = W.Douyin()
    lib_idx, lib_folders = W.load_library_index()
    douk_ids = W.douk_downloaded_ids()
    print("DouK 下载记录 %d 条（里面有、本地没有的，视为你删过，不会再补）" % len(douk_ids))
    total = [0, 0, 0, 0]
    for uid, a in authors.items():
        print("\n" + "=" * 64)
        print("作者：%s  uid=%s" % (a["nickname"], uid))
        r = backfill_one(dy, lib_idx, lib_folders, uid,
                         a["sec_uid"], a["nickname"], args.pages, douk_ids)
        total = [x + y for x, y in zip(total, r)]

    print("\n" + "=" * 64)
    print("全部完成：新增 %d 条  跳过 %d 条  失败 %d 条  已删不补 %d 条" % tuple(total))
    return 0 if total[2] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
