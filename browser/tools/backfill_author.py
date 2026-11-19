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


def backfill_one(dy, lib_idx, lib_folders, uid, sec, nick, pages, douk_ids=None,
                 fill_dups=False):
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
    # 和 scan_round 同一口径：数出「这一天接口上有几条描述剥完装饰符是一样的」，
    # 供 is_known 的「只差表情」那档做唯一性校验（见 watch_core.is_known 方向三）
    bare_cnt = {}
    exact_cnt = {}      # 同一天「描述一模一样」的条数：>1 时文件名没有身份
    for _a in al:
        _ct = int(_a.get("create_time") or 0)
        if not _ct:
            continue
        _d = datetime.fromtimestamp(_ct).strftime("%Y-%m-%d")
        _kd = W.key_of_desc(_a.get("desc") or "", 64)
        _k = (_d, W.deco_strip(_kd))
        bare_cnt[_k] = bare_cnt.get(_k, 0) + 1
        _ke = (_d, _kd)
        exact_cnt[_ke] = exact_cnt.get(_ke, 0) + 1
    print("接口返回 %d 条 | 本地索引 %d 条 | 目录 %s" % (len(al), len(local), folder))
    # 文件名里带作品 ID 的那些（描述为空的作品只能靠它认，见 watch_core.local_aids）
    disk_ids = W.local_aids(author_dir)
    if disk_ids:
        print("本地文件名里带作品 ID 的 %d 条" % len(disk_ids))

    dl = sk = fail = gone = 0
    misses = []
    for a in al:
        ct = int(a.get("create_time") or 0)
        date = datetime.fromtimestamp(ct).strftime("%Y-%m-%d")
        desc = a.get("desc") or ""
        aid = str(a.get("aweme_id") or "")
        _dk = W.key_of_desc(desc, 64)
        _n_same = exact_cnt.get((date, _dk), 1)
        _loc_same = sum(1 for k in (date_idx.get(date) or ())
                        if W.strip_aid_tail(k) == _dk)
        # 同一天有多条一模一样的描述（`#ootd` 那种）：文件名没有身份，
        # 下载器（和 DouK）会把它们写到同一个路径 → 只剩最后一条。
        # --fill-dups：这种情况下不看「DouK 下过 = 你删了」，按缺的条数补齐。
        _dup_day = (fill_dups and _n_same > 1 and _loc_same < _n_same)
        # **第一道判据：本地文件名的 ID 就是他**。描述为空的作品在 is_known() 里
        # 没有名字可比（直接返回 False），以前会一路滚到「DouK 下过、本地没有」，
        # 被误当成「你删过」——盘上明明还在（2026-11-13 用户看到成片「已删不补」）。
        if aid and aid in disk_ids:
            sk += 1
            continue
        # **第二道判据：同一天、同一描述，本地条数够就算都在。**
        # 描述为空的作品名字里既没字也没 ID（DouK 落成 `日期-昵称.mp4`），名字层认不出来，
        # 只能按条数对账：接口这天有 3 条、本地也有 3 条 → 都在，谁也别补。
        # 少一条才是真缺（同名覆盖），交给下面的 _dup_day 决定要不要补。
        if _loc_same >= _n_same:
            sk += 1
            continue
        if not _dup_day and W.is_known(local, date_idx, date, desc, 64,
                                       bare_total=bare_cnt.get((date, W.deco_strip(_dk)), 1),
                                       same_total=_n_same):
            sk += 1
            continue
        # DouK 记录里有、本地也没有（名字层和 ID 层都没认出来）= 大概率你自己删掉的。
        # **别再补回来**：删作品就是为了不要它，补下载工具不该跟用户对着干。
        # （同一天重名的那批例外：那是撞名覆盖，不是用户删的，见上面的 _dup_day）
        if not _dup_day and aid in douk_ids:
            gone += 1
            print("  - %s  %s  （DouK 有记录、本地也没有 → 当你删过，跳过）"
                  % (date, desc[:40]))
            continue
        misses.append(date)
        files, size, err = W.download_aweme(dy, a, author_dir, nick, 64)
        if files:
            dl += 1
            disk_ids.add(aid)
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
    print("汇总：新增 %d  跳过 %d  失败 %d  DouK有但本地无 %d  (接口总数 %d)"
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
    ap.add_argument("--fill-dups", action="store_true",
                    help="补「同一天多条同名作品」：这种日子下载器会写到同一个文件名、"
                         "只留最后一条，DouK 记录里却三条都有。开了它就不把这些当成"
                         "「你删过」，按本地少的条数补回来（2026-11-13）")
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
                         a["sec_uid"], a["nickname"], args.pages, douk_ids,
                         fill_dups=args.fill_dups)
        total = [x + y for x, y in zip(total, r)]

    print("\n" + "=" * 64)
    print("全部完成：新增 %d 条  跳过 %d 条  失败 %d 条  已删不补 %d 条" % tuple(total))
    return 0 if total[2] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
