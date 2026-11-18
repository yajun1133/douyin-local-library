#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把挂着「扫不到」标签的作者重新探一遍，按结果落两种状态。

背景（2026-11-13 用户口径）：
  以前「连续 3 次扫不到」是**取消追更**，人被移出名单，标签留着做纪念。
  现在改成**停用**：人留在名单里、归到「已停用」，每 7 天自动回探一枪。
  所以历史那批「被移出 + 打标签」的作者要补一次：先真的再查一遍 ——
  能查到作品的恢复追更（顺手摘标签），还是空的就补进名单并置为停用。

跑法（必须 3.12：curl_cffi / ABogus 是那个版本编译的）：
    PYTHONPATH= C:/Python312/python.exe tools/backfill_noapi_states.py          # 只看结果，不动文件
    PYTHONPATH= C:/Python312/python.exe tools/backfill_noapi_states.py --apply  # 真写

⚠️ 跑之前先停掉追更常驻进程：它会整份覆盖 watch_state.json，脚本写的
   no_api 记录会被冲掉（2026-11-13 踩过一次）。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import paths      # noqa: E402
import watch_core as WC   # noqa: E402

TAG = WC.NO_API_TAG


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def tagged_uids():
    """user_data.json 里带「扫不到」标签的所有 uid。"""
    d = load_json(paths.USER_DATA, {}) or {}
    accs = d.get("accounts") or {}
    out = []
    for uid, rec in accs.items():
        if isinstance(rec, dict) and TAG in (rec.get("tags") or []):
            out.append(str(uid))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写文件（不加只打印报告）")
    ap.add_argument("--gap", type=float, default=2.0, help="每位作者之间的间隔秒数")
    args = ap.parse_args()

    watch = WC.load_watch()
    cfg = watch["settings"]
    state = WC.load_state()
    data = load_json(paths.DATA_JSON, {}) or {}
    accs = data.get("accounts") or {}
    watchers = {str(a.get("uid")): a for a in (watch.get("authors") or [])}

    uids = tagged_uids()
    print("带「%s」标签的作者：%d 位" % (TAG, len(uids)))
    if not uids:
        return 0

    dy = WC.Douyin()
    pool = getattr(dy, "pool", None)

    ok, bad, skip = [], [], []
    for i, uid in enumerate(uids, 1):
        rec = accs.get(uid) or {}
        sec = (rec.get("sec_uid") or "").strip()
        nick = (rec.get("nickname") or "").strip() or uid
        local = len(rec.get("works") or [])
        w = watchers.get(uid)
        if not sec:
            skip.append((nick, uid, "没有 SEC_UID，探测不了"))
            print("  %2d. %-14s 跳过：没有 SEC_UID" % (i, nick[:14]))
            continue
        # 先打一枪；空的话再走「同号一次 + 换号一次」的补救（和 scan_round 一个口径）
        try:
            got = dy.author_works(sec, pages=int(cfg.get("per_author_pages") or 1))
            tries = 1
            why = ""
            if not got:
                got, tries, why = WC._retry_empty(
                    dy, sec, nick, pool, cfg, int(cfg.get("per_author_pages") or 1))
        except Exception as e:
            got, tries, why = [], 0, "查询出错：%s" % str(e)[:60]
        stamp = time.strftime("%H:%M:%S")
        if got:
            newest = max(int(a.get("create_time") or 0) for a in got) or 0
            print("  %2d. [%s] ✓ %-14s 扫到了 %d 条（最新 %s）"
                  % (i, stamp, nick[:14], len(got),
                     time.strftime("%m-%d %H:%M", time.localtime(newest)) if newest else "?"))
            ok.append((uid, nick, sec, local))
        else:
            print("  %2d. [%s] ✗ %-14s 仍然空（试了 %d 次：%s）"
                  % (i, stamp, nick[:14], tries, why))
            bad.append((uid, nick, sec, local, why))
        time.sleep(max(0.5, args.gap))

    print("\n扫到了 %d 位 / 仍然空 %d 位 / 跳过 %d 位" % (len(ok), len(bad), len(skip)))
    if not args.apply:
        print("\n这是预演，没有写任何文件。加 --apply 才落盘：")
        print("  · 扫到的 %d 位 → 恢复追更 + 摘「%s」标签" % (len(ok), TAG))
        print("  · 空的 %d 位 → 补进名单并置为「停用」，标签保留，7 天后自动回探" % len(bad))
        return 0

    now = time.time()
    nows = time.strftime("%Y-%m-%d %H:%M:%S")
    probe_days = float(cfg.get("no_api_probe_days") or 7)
    limit = int(cfg.get("no_api_drop_rounds") or 3)

    # —— 扫到的：恢复追更 + 摘标签 ——
    for uid, nick, sec, local in ok:
        w = WC.load_watch()
        hit = next((a for a in (w.get("authors") or []) if str(a.get("uid")) == uid), None)
        if hit is None:
            w.setdefault("authors", []).append({
                "uid": uid, "sec_uid": sec, "nickname": nick,
                "added_at": nows, "enabled": True, "interval": 0,
                # 之前被移出过，基线可能停在那儿：让他先补一遍现有作品
                "full_scan": True,
            })
            print("  ＋ %s 回到名单（启用）" % nick)
        elif not hit.get("enabled", True):
            hit["enabled"] = True
            print("  ▶ %s 恢复启用" % nick)
        WC.save_watch(w)
        WC.tag_author(uid, TAG, False)
        st = WC.load_state()
        (st.get("no_api") or {}).pop(uid, None)
        WC.save_state(st)

    # —— 仍然空的：进名单、置停用、排 7 天回探 ——
    for uid, nick, sec, local, why in bad:
        w = WC.load_watch()
        hit = next((a for a in (w.get("authors") or []) if str(a.get("uid")) == uid), None)
        if hit is None:
            w.setdefault("authors", []).append({
                "uid": uid, "sec_uid": sec, "nickname": nick,
                "added_at": nows, "enabled": False, "interval": 0,
            })
            print("  ＋ %s 补进名单（停用）" % nick)
        elif hit.get("enabled", True):
            hit["enabled"] = False
            print("  ⏸ %s 置为停用" % nick)
        WC.save_watch(w)
        WC.tag_author(uid, TAG, True)
        st = WC.load_state()
        st.setdefault("no_api", {})[uid] = {
            "nickname": nick, "local": local, "tries": 3, "rounds": max(limit, 3),
            "ts": now, "reason": why or "连查三次都返回空",
            "next_probe": now + probe_days * 86400,
        }
        WC.save_state(st)

    print("\n写入完成：恢复 %d 位，停用 %d 位" % (len(ok), len(bad)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
