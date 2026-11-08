# -*- coding: utf-8 -*-
"""补下图文作品里被漏掉的「实况视频」。

背景（2026-11-08 查到的坑）：
    抖音图文作品（aweme_type=68）的**每一项**都可能是实况/动图 —— 那一项自带
    `video.play_addr`，而它的 `url_list` 只是封面帧。2026-11-08 之前的追更下载器
    只认 `url_list`，于是实况项被存成了一张静态图：界面上「第一条是图、第二条也
    是图」，第二条怎么点都播不出来。DouK 下的没这个问题，所以对比之下很像
    「用了追更才坏的」。

用法：
    # 先停追更常驻，避免两边同时打接口（面板上点停止，或 POST /api/watch stop）
    python tools/backfill_live.py <uid> [<uid> ...] [--pages 3] [--repair] [--dry-run]

    --pages    每位作者翻几页（默认 3，一页 20 条）
    --repair   把缺视频的作品整条移进回收站再重下（推荐：不会留下多余的封面图）
    --dry-run  只报告缺哪些，不动任何文件

为什么默认只报告、--repair 才动手：
    本地文件名可能是老版 DouK 用省略号截断过的（`我来了 #卡点舞 #CU...度激励计划`），
    跟接口的完整描述字面不同（长描述被吃成 `前缀...后缀`）。判重必须走 watch_core 的省略号模糊匹配，否则会把
    「本地已经有视频」的作品误判成缺失 —— 2026-11-08 就因此误删过两条。
"""
import os
import re
import sys
import json
import argparse
import urllib.request
from datetime import datetime

BASE = os.getcwd()                                  # 必须在构造 Douyin 之前固定：
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))   # 下载器初始化会
                                                                    # os.chdir 到 DouK 目录
import paths                      # noqa: E402
import watch_core as W            # noqa: E402

API = "http://127.0.0.1:8723/api/works/delete"
SEG_RE = re.compile(r"^(.+)_(\d+)$")


def load_index():
    """uid -> [nickname, folder, sec_uid]"""
    out = {}
    try:
        ud = json.load(open(paths.USER_DATA, encoding="utf-8"))
        acc = ud.get("accounts") if isinstance(ud, dict) and "accounts" in ud else ud
        for uid, a in (acc or {}).items():
            if isinstance(a, dict):
                out[str(uid)] = [a.get("nickname") or "", a.get("folder") or "",
                                 a.get("sec_uid") or ""]
    except Exception as e:
        print("读 user_data 失败：%s" % e)
    try:
        wd = json.load(open(os.path.join(paths.DATA_DIR, "watch.json"), encoding="utf-8"))
        for a in (wd.get("authors") or []):
            uid = str(a.get("uid") or "")
            if not uid:
                continue
            rec = out.setdefault(uid, ["", "", ""])
            rec[0] = rec[0] or a.get("nickname") or ""
            rec[1] = rec[1] or a.get("folder") or ""
            rec[2] = rec[2] or a.get("sec_uid") or ""
    except Exception:
        pass
    return out


def ell_same(a, b):
    """两个 work_key 是不是同一条（容忍老文件名的省略号截断）。"""
    if a == b:
        return True
    for k1, k2 in ((a, b), (b, a)):
        sp = W.ellipse_split(k1)
        if sp and W._ell_hit(k2, sp[0], sp[1]):
            return True
    return False


def scan_dir(author_dir, date):
    """把作者目录里某一天的文件按「作品基名」聚起来：{完整 stem: {序号: {后缀: 路径}}}

    文件名形如 `2026-11-07-{昵称}-{描述}[_{序号}].ext`。这里**不切**昵称和描述：
    日期本身带 `-`、昵称也可能带 `-`，切早了必然切错。留到和接口描述比对时再试。
    """
    out = {}
    if not os.path.isdir(author_dir):
        return out
    pref = date + "-"
    for fn in os.listdir(author_dir):
        if not fn.startswith(pref):
            continue
        stem, ext = os.path.splitext(fn)
        m = SEG_RE.match(stem)
        base, idx = (m.group(1), int(m.group(2))) if m else (stem, 0)
        out.setdefault(base, {}).setdefault(idx, {})[ext.lower()] = os.path.join(author_dir, fn)
    return out


def desc_candidates(stem, date, nick):
    """从完整 stem 里猜出「描述」部分的所有可能。

    `2026-11-01-某昵称-开头一段...结尾一段` → 候选含
        `某昵称-开头一段...结尾一段`
        `开头一段...结尾一段`                    （按昵称切）
        `一段...结尾一段` …                      （按每个 `-` 切）
    只要有一个能和接口描述对上就算中了。
    """
    s = stem[len(date) + 1:] if stem.startswith(date + "-") else stem
    out = [s]
    n = W.clean_part(nick) or ""
    if n and s.startswith(n + "-"):
        out.insert(0, s[len(n) + 1:])
    i = -1
    while True:
        i = s.find("-", i + 1)
        if i < 0:
            break
        out.append(s[i + 1:])
    return out


def find_group(groups, date, nick, desc):
    """在当天已下载的作品里找这条（精确 key 优先，再走省略号模糊）。"""
    key = W.work_key(desc, 64)
    for base, items in groups.items():
        for cand in desc_candidates(base, date, nick):
            if W.work_key(cand, 64) == key:
                return base, items
    for base, items in groups.items():
        for cand in desc_candidates(base, date, nick):
            if ell_same(W.work_key(cand, 64), key):
                return base, items
    return None, None


def live_indexes(aweme):
    """这一条作品里，哪几项其实是视频（实况/动图）。"""
    imgs = aweme.get("images") or ((aweme.get("image_post_info") or {}).get("images") or [])
    return [i for i, im in enumerate(imgs, 1)
            if W.pick_best_video((im or {}).get("video") or {})]


def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=600).read().decode())


def works_index(lib_works, date, desc, nick=""):
    """在 data.json 的 works 里定位这条作品的下标（同一天内用 key 匹配）。"""
    key = W.work_key(desc, 64)
    for i, w in enumerate(lib_works):
        if str(w.get("date")) != date:
            continue
        d = w.get("desc")
        if d:
            cands = [str(d)]
        else:                              # 老条目没有 desc，只能从文件名反推
            rel = ((w.get("files") or [{}])[0] or {}).get("rel") or ""
            stem = SEG_RE.sub(r"\1", os.path.splitext(os.path.basename(rel))[0])
            cands = desc_candidates(stem, date, nick)
        for c in cands:
            k = W.work_key(c, 64)
            if k == key or ell_same(k, key):
                return i
    return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uids", nargs="+")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--repair", action="store_true", help="缺的整条进回收站再重下")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    info = load_index()
    dy = W.Douyin()
    lib = json.load(open(paths.DATA_JSON, encoding="utf-8"))
    tot_miss = tot_fix = tot_fail = 0

    for uid in args.uids:
        nick, folder, sec = (info.get(str(uid)) or ["", "", ""])
        if not sec:
            print("× %s 没有 sec_uid（既不在追更名单也不在用户库），跳过" % uid)
            continue
        if not folder:
            folder = "UID%s_%s_发布作品" % (uid, W.clean_part(nick) or "未知作者")
        author_dir = os.path.join(paths.SCAN_ROOT, folder)
        print("\n===== %s (%s) =====" % (nick or uid, uid))
        print("  目录：%s" % author_dir)

        try:
            aws = dy.author_works(sec, pages=args.pages)
        except Exception as e:
            print("  × 拉作品列表失败：%s" % str(e)[:100]); tot_fail += 1
            continue

        lib_works = ((lib.get("accounts") or {}).get(str(uid)) or {}).get("works") or []
        targets = []
        for aw in aws:
            idxs = live_indexes(aw)
            ct = int(aw.get("create_time") or 0)
            if not idxs or not ct:
                continue
            date = datetime.fromtimestamp(ct).strftime("%Y-%m-%d")
            desc = aw.get("desc") or ""
            base, items = find_group(scan_dir(author_dir, date), date, nick, desc)
            if not items:
                # 本地压根没有这一条 —— 属于「历史漏抓」，不是本工具管的「缺视频」。
                # 那种情况交给 tools/backfill_author.py，别在这里顺手补（会绕开它的判重）。
                continue
            lack = [i for i in idxs if not (i in items and ".mp4" in items[i])]
            if lack:
                n_local = len(items) if items else 0
                n_remote = len(aw.get("images") or
                               ((aw.get("image_post_info") or {}).get("images") or []))
                targets.append((aw, date, desc, lack, base, items, n_local, n_remote))

        if not targets:
            print("  ✓ 没有缺视频的作品"); continue
        tot_miss += len(targets)
        for aw, date, desc, lack, base, items, n_local, n_remote in targets:
            print("  ! %s 实况项 %s 缺视频  [本地 %d 项 / 接口 %d 项]  %s"
                  % (date, lack, n_local, n_remote, (desc or "")[:34]))
            if args.dry_run:
                continue
            if not args.repair:
                n, size, err = W.download_aweme(dy, aw, author_dir, nick)
                print("      补下 %d 个 / %.1f MB %s" % (n, size / 1024.0 ** 2, err or ""))
                tot_fix += 0 if err else 1
                tot_fail += 1 if err else 0
                print("      · 若同一项还留着封面图（_%s.webp/.jpeg），需要手动清掉"
                      % "/".join(str(i) for i in lack))
                continue
            # --repair：整条进回收站，再用修好的下载器重下，避免残留封面图
            wi = works_index(lib_works, date, desc, nick)
            if wi < 0:
                print("      × 在库索引里找不到这条，跳过（先让扫描器跑一轮）")
                tot_fail += 1; continue
            try:
                r = post(API, {"uid": str(uid), "idxs": [wi]})
            except Exception as e:
                print("      × 删除失败：%s" % str(e)[:90]); tot_fail += 1; continue
            if not r.get("ok"):
                print("      × 删除被拒：%s" % str(r)[:90]); tot_fail += 1; continue
            print("      已进回收站（批次 %s，%d 个文件）"
                  % (r.get("batch"), r.get("files") or 0))
            n, size, err = W.download_aweme(dy, aw, author_dir, nick)
            if err:
                print("      × 重下出错：%s" % err[:90]); tot_fail += 1
            else:
                print("      ✓ 重下 %d 个 / %.1f MB" % (n, size / 1024.0 ** 2)); tot_fix += 1
            lib_works = lib_works[:wi] + lib_works[wi + 1:]

    print("\n=== 汇总：缺 %d 条，处理 %d 条，失败 %d 条%s ===" %
          (tot_miss, tot_fix, tot_fail,
           "（dry-run，未改动）" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
