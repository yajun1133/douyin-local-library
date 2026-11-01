#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理缩略图 / 转码缓存（不启动服务也能用）。

两个缓存目录都只是加速用的中间产物，删了会按需重建：
    thumb_cache\\  视频首帧缩略图（重建一瞬）
    trans_cache\\  HEVC→H.264 转码产物（重建几秒）

用法:
    python clean_cache.py             # 按上限淘汰最久没用的（上限见 server.py 的
                                      # THUMB_CACHE_MAX_MB / TRANS_CACHE_MAX_MB）
    python clean_cache.py --wipe      # 全部清空
    python clean_cache.py --thumb     # 只处理缩略图
    python clean_cache.py --trans     # 只处理转码缓存
    python clean_cache.py --status    # 只看占用，不删

上限和 server.py 里的 THUMB_CACHE_MAX_MB / TRANS_CACHE_MAX_MB 共用同一份常量。
"""

import os
import sys

# 代码在 ../src/：复用 server.py 的 trim_cache / wipe_cache / 上限常量，
# 避免「脚本一套、服务一套」跑偏
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import server as S   # noqa: E402


def human(n):
    n = int(n or 0)
    if n >= 1024 ** 3:
        return "%.2f GB" % (n / 1024 ** 3)
    if n >= 1024 ** 2:
        return "%.1f MB" % (n / 1024 ** 2)
    if n >= 1024:
        return "%d KB" % round(n / 1024)
    return "%d B" % n


def show():
    t = S.dir_stats(S.THUMB_CACHE)
    r = S.dir_stats(S.TRANS_CACHE)
    print("  缩略图 thumb_cache : %5d 个  %9s  (上限 %d MB)" % (t["files"], human(t["bytes"]), S.THUMB_CACHE_MAX_MB))
    print("  转码   trans_cache : %5d 个  %9s  (上限 %d MB)" % (r["files"], human(r["bytes"]), S.TRANS_CACHE_MAX_MB))
    c = S.dir_stats(S.COVER_CACHE)
    print("  封面   cover_cache : %5d 个  %9s  (受保护，永不清理)" % (c["files"], human(c["bytes"])))
    print("  合计（可清理部分）  :          %9s" % human(t["bytes"] + r["bytes"]))
    return t, r


def main():
    args = sys.argv[1:]
    only = None
    if "--thumb" in args and "--trans" not in args:
        only = "thumb"
    elif "--trans" in args and "--thumb" not in args:
        only = "trans"
    targets = []
    if only in (None, "thumb"):
        targets.append(("缩略图", S.THUMB_CACHE, S.THUMB_CACHE_MAX_MB))
    if only in (None, "trans"):
        targets.append(("转码缓存", S.TRANS_CACHE, S.TRANS_CACHE_MAX_MB))

    print("清理前：")
    show()
    if "--status" in args:
        return 0

    wipe = "--wipe" in args
    print("\n%s..." % ("清空" if wipe else "按上限淘汰"))
    total = 0
    for name, path, cap in targets:
        # 缩略图里可能有「视频封面」用到的同款首帧，淘汰时留一命
        # （封面的正式副本在 cover_cache，这里只是保险）
        keep = S.cover_cache_names() if path == S.THUMB_CACHE else ()
        r = S.wipe_cache(path, keep=keep) if wipe else S.trim_cache(path, cap, keep=keep)
        total += r["freed"]
        extra = "，%d 个删不掉（可能正被占用）" % r["failed"] if r["failed"] else ""
        print("  %s：删了 %d 个，释放 %s%s" % (name, r["removed"], human(r["freed"]), extra))

    print("\n清理后：")
    show()
    print("\n本次共释放 %s" % human(total))
    print("封面目录 cover_cache 不在清理范围内，一张都没动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
