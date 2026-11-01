#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SEC_UID 体检与修复 —— 找出「sec_uid 指向了别人」的账号。

为什么会有错配：早期 import_secuid.py 是从 xlsx 按**昵称**匹配回填的，
两个同名的作者（如两个「嘿嘿」）会被填上同一条 sec_uid，看起来数据齐全，
点开主页却是别人。昵称会改，所以唯一可靠的判据是**拿 uid 去抖音反查真正的
sec_uid**（uid 和 sec_uid 都不会变）。

判据（两条，都不用猜）：
  1) 重复：同一个 sec_uid 挂在两个不同 uid 上 —— 至少一条是错的；
  2) 反查：GET aweme/v1/web/user/profile/other/?user_id=<uid> 返回的 sec_uid
     与本地不一致 —— 本地那条是错的。

**修复只动 sec_uid 一个字段**（走 server.merge_save_user_data 合并写 + 原子替换），
备注 / 别名 / 简介 / 标签 / 星标 / 历史记录 / 大小号 一个字都不碰。

用法（必须用 Python 3.12，curl_cffi 只有 3.12 版本）：
    python tools\\secuid_check.py --dry --by-secuid    全量批量反向（首选，73 个请求约 2 分钟）
    python tools\\secuid_check.py --dry --dup-only     只查「一个 sec_uid 挂多个 uid」的组
    python tools\\secuid_check.py --dry                逐 uid 正向反查（慢，1.6 秒/个）
    python tools\\secuid_check.py --fix --dup-only     按核查结果修（先 --dry 看一遍）
    python tools\\secuid_check.py --dry --limit 50     只查前 50 个，试水

核查结果缓存到 data/secuid_check.json，重跑会跳过已确认正确的账号（--fresh 忽略缓存）。
缓存里同时存了抖音侧的 `nick` / `dy_id`，可以顺手核对「本地 douyin_id ≠ 抖音 unique_id」
——按昵称回填的年代，这两列是一起抄错的（如两个同名作者都写成同一个抖音号）。
"""

import argparse
import json
import os
import random
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))          # browser/tools
_ROOT = os.path.dirname(_HERE)                              # browser
sys.path.insert(0, os.path.join(_ROOT, "src"))              # paths / server
sys.path.insert(0, os.path.join(_HERE, "legacy"))           # fetch_uid_by_secuid

import paths                                                # noqa: E402

CACHE_FILE = os.path.join(paths.DATA_DIR, "secuid_check.json")
PROFILE_URL = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
PARAMS = {"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
          "version_code": "170400", "version_name": "17.4.0"}


def out(msg=""):
    try:
        sys.stdout.write(str(msg) + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:                      # 老控制台兜底
        sys.stdout.write(str(msg).encode("utf-8", "replace").decode("utf-8", "replace") + "\n")


def load_accounts():
    with open(paths.USER_DATA, "r", encoding="utf-8") as fp:
        return (json.load(fp).get("accounts") or {})


def load_cache():
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as fp:
            d = json.load(fp)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(cache, fp, ensure_ascii=False, indent=1)
    os.replace(tmp, CACHE_FILE)


def find_dups(acc):
    """同一个 sec_uid 挂在多个 uid 上 → [(sec, [uid, ...])]"""
    by = {}
    for uid, a in acc.items():
        s = (a.get("sec_uid") or "").strip()
        if s:
            by.setdefault(s, []).append(uid)
    return [(s, u) for s, u in by.items() if len(u) > 1]


def make_client():
    import fetch_uid_by_secuid as F
    F.init_env()
    from curl_cffi import requests as cr
    cookie = F.load_cookie()
    if not cookie.get("sessionid"):
        out("!! settings.json 里没有有效 cookie，联网核查不可用")
        sys.exit(2)
    return cr, cookie, F


def query_uid(cr, cookie, F, uid):
    """uid → {real, nick, dy_id}；查不到时 real 为空。"""
    try:
        resp = cr.get(PROFILE_URL, params=dict(PARAMS, user_id=uid),
                      headers={"User-Agent": F.UA, "Referer": "https://www.douyin.com/"},
                      cookies=cookie, impersonate="chrome131", timeout=30)
        user = (json.loads(resp.text) or {}).get("user") or {}
        return {"real": (user.get("sec_uid") or "").strip(),
                "nick": user.get("nickname") or "",
                "dy_id": user.get("unique_id") or user.get("short_id") or "",
                "at": time.strftime("%Y-%m-%d %H:%M")}
    except Exception as e:
        return {"real": "", "error": str(e), "at": time.strftime("%Y-%m-%d %H:%M")}


def audit_by_secuid(cr, cookie, F, acc, cache, gap=0.8):
    """批量模式（快 20 倍）：一次 20 个 sec_uid 查「这个 sec_uid 到底属于哪个 uid」。

    正向（逐 uid 查）要 1400 多次请求；这里只用 70 多次，2 分钟跑完全库。

    判定：本地把 sec_uid S 挂在 uid A 上，若 S 在抖音上属于 uid B（B≠A），
    则 A 这条是错的。反过来说，同一条 S 被多人挂时，只有 B 是对的。
    """
    pairs = [(u, (a.get("sec_uid") or "").strip()) for u, a in acc.items()
             if isinstance(a, dict) and (a.get("sec_uid") or "").strip()]
    secs = sorted({s for _, s in pairs})
    out("待批量核查 %d 个 sec_uid（%d 个账号，约 %d 批）"
        % (len(secs), len(pairs), (len(secs) + 19) // 20))

    owner = {}
    BATCH = 20
    for i in range(0, len(secs), BATCH):
        chunk = secs[i:i + BATCH]
        try:
            owner.update(F.query_batch(cr, cookie, chunk))
        except Exception as e:
            out("  批次 %d 失败：%s" % (i // BATCH + 1, e))
        if (i // BATCH + 1) % 10 == 0:
            out("  进度 %d/%d" % (i + len(chunk), len(secs)))
        time.sleep(gap)

    wrong = []
    for uid, sec in pairs:
        hit = owner.get(sec) or {}
        real_uid = str(hit.get("uid") or "")
        cache[uid] = {
            "local": sec,
            "real": sec if real_uid == str(uid) else "",   # 归属正确时先记为同值
            "nick": hit.get("nickname") or "",
            "dy_id": hit.get("unique_id") or hit.get("short_id") or "",
            "owner_uid": real_uid,
            "local_nick": acc[uid].get("nickname") or "",
            "at": time.strftime("%Y-%m-%d %H:%M"),
        }
        if real_uid and real_uid != str(uid):
            wrong.append(uid)
    save_cache(cache)
    out("\n归属正确 %d，确认错配 %d，抖音查不到 %d"
        % (len(pairs) - len(wrong) - sum(1 for u, _ in pairs if not cache[u].get("owner_uid")),
           len(wrong),
           sum(1 for u, _ in pairs if not cache[u].get("owner_uid"))))

    if not wrong:
        return []
    # 错配的这几条，再逐条反查「它自己真正的 sec_uid」（数量很少）
    out("\n对 %d 条错配逐个反查正确 sec_uid…" % len(wrong))
    for uid in wrong:
        hit = query_uid(cr, cookie, F, uid)
        cache[uid].update({"real": hit.get("real") or "", "probe_nick": hit.get("nick") or "",
                           "probe_id": hit.get("dy_id") or "", "error": hit.get("error", "")})
        time.sleep(gap)
    save_cache(cache)
    return wrong



def plan_fix(acc, cache):
    """按核查结果给出修复清单 {uid: new_sec}。real 为空（查不到）的一律跳过。"""
    fixes = {}
    for uid, a in acc.items():
        local = (a.get("sec_uid") or "").strip()
        hit = cache.get(uid) or {}
        real = (hit.get("real") or "").strip()
        if real and real != local:
            fixes[uid] = real
    return fixes


def apply_fixes(acc, fixes):
    """只提交 sec_uid 一个字段，其余字段由服务端合并写原样保留。"""
    if not fixes:
        out("没有需要修的。")
        return 0
    sys.path.insert(0, os.path.join(_ROOT, "src"))
    import server
    partial = {"accounts": {uid: {"sec_uid": s} for uid, s in fixes.items()}}
    fixed = server.merge_save_user_data(partial)
    out("已写入 %d 条 sec_uid（顺带归一化大小号 %d 条）" % (len(fixes), fixed))
    return len(fixes)


def verify(acc, cache):
    """复查：仍重复 / 仍不一致。"""
    after = load_accounts()
    dups = find_dups(after)
    bad = [(uid, (after.get(uid) or {}).get("sec_uid", "")) for uid in cache
           if (cache[uid].get("real") or "").strip()
           and (after.get(uid) or {}).get("sec_uid", "") != cache[uid]["real"]]
    out("\n复查：重复组 %d，仍与抖音不一致 %d" % (len(dups), len(bad)))
    for s, uids in dups:
        out("  重复 %s -> %s" % (s[:20] + "…", [(u, after[u].get("nickname")) for u in uids]))
    return len(dups) == 0 and len(bad) == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只看报告，不写盘")
    ap.add_argument("--fix", action="store_true", help="按核查结果修正")
    ap.add_argument("--dup-only", action="store_true", help="只处理重复 sec_uid 的账号")
    ap.add_argument("--by-secuid", action="store_true",
                    help="批量模式（推荐）：反查「这个 sec_uid 属于谁」，70 多次请求跑完全库")
    ap.add_argument("--limit", type=int, default=0, help="最多核查多少个（试水用）")
    ap.add_argument("--fresh", action="store_true", help="忽略核查缓存，全部重查")
    ap.add_argument("--gap", type=float, default=0.55, help="每个请求间隔秒（默认 0.55）")
    args = ap.parse_args()

    acc = load_accounts()
    dup_secs = find_dups(acc)
    out("账号 %d 个，其中 sec_uid 重复组 %d（涉及 %d 个 uid）"
        % (len(acc), len(dup_secs), sum(len(u) for _, u in dup_secs)))
    for s, uids in dup_secs:
        out("  %s… -> %s" % (s[:20], [(u, acc[u].get("nickname")) for u in uids]))

    # 待核查名单
    cache = load_cache()
    if args.by_secuid:
        # 批量模式：反方向查「这个 sec_uid 属于谁」，70 多次请求跑完全库
        cr, cookie, F = make_client()
        audit_by_secuid(cr, cookie, F, acc, cache, gap=max(args.gap, 0.6))
        todo = []
    elif args.dup_only:
        todo = [u for _, uids in dup_secs for u in uids]
    else:
        todo = [u for u, a in acc.items() if (a.get("sec_uid") or "").strip()]
    if not args.fresh:
        todo = [u for u in todo if not (cache.get(u) or {}).get("real")]
    if args.limit:
        todo = todo[:args.limit]

    out("\n待联网核查 %d 个（缓存已有 %d 个）" % (len(todo), len(cache)))
    if todo:
        cr, cookie, F = make_client()
        for i, uid in enumerate(todo, 1):
            hit = query_uid(cr, cookie, F, uid)
            cache[uid] = dict(hit, local=(acc[uid].get("sec_uid") or "").strip(),
                              local_nick=acc[uid].get("nickname") or "")
            if i % 20 == 0 or i == len(todo):
                save_cache(cache)
                out("  进度 %d/%d" % (i, len(todo)))
            time.sleep(args.gap + random.random() * 0.2)
        save_cache(cache)

    # 报告
    out("\n%-20s %-12s %-14s %-14s %s" % ("uid", "本地名", "本地sec", "真实sec", "抖音名/抖音号"))
    bad = []
    for uid, hit in cache.items():
        real = (hit.get("real") or "").strip()
        local = (hit.get("local") or "").strip()
        if not real:
            out("%-20s %-12s %-14s %-14s 查不到（%s）"
                % (uid, (hit.get("local_nick") or "")[:10], local[:12], "—", hit.get("error", "空 user")))
            continue
        if real != local:
            bad.append(uid)
            out("%-20s %-12s %-14s %-14s %s / %s  ✗"
                % (uid, (hit.get("local_nick") or "")[:10], local[:12], real[:12],
                   hit.get("nick"), hit.get("dy_id")))
    out("\n不一致 %d 条 / 已核查 %d 条" % (len(bad), sum(1 for h in cache.values() if h.get("real"))))

    if args.fix:
        fixes = plan_fix(acc, cache)
        out("\n准备修 %d 条（只改 sec_uid 字段，其它标注不动）" % len(fixes))
        apply_fixes(acc, fixes)
        verify(acc, cache)
    else:
        out("\n（这是 --dry 模式，没有写任何东西。确认无误后加 --fix）")


if __name__ == "__main__":
    main()
