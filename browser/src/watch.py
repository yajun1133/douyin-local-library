#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""追更命令行（Python 3.12）。

    C:\\Python312\\python.exe src\\watch.py --list                  # 看名单
    C:\\Python312\\python.exe src\\watch.py --add 123456 MS4wLjAB...  # 加作者（uid / sec_uid / 主页链接都行）
    C:\\Python312\\python.exe src\\watch.py --add-top 50            # 从本地库挑作品最多的 50 位加入
    C:\\Python312\\python.exe src\\watch.py --remove 123456         # 移出名单
    C:\\Python312\\python.exe src\\watch.py --scan --dry            # 试运行：只说会下什么，不真下
    C:\\Python312\\python.exe src\\watch.py --scan                  # 扫一轮
    C:\\Python312\\python.exe src\\watch.py --loop                  # 常驻（按分级节奏自己调度）
    C:\\Python312\\python.exe src\\watch.py --status                # 看每个作者的状态

参数在 data/watch.json 的 settings 里，第一次运行会自动写一份带默认值的。
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths      # noqa: E402
import watch_core as W  # noqa: E402

SEC_RE = re.compile(r"(MS4wLjAB[\w\-]{16,})")


def norm_sec(s):
    """从一整条主页网址里把 sec_uid 抠出来；本来就是纯 id 也认。"""
    s = str(s or "").strip()
    m = SEC_RE.search(s)
    return m.group(1) if m else ""


def load_accounts():
    try:
        with open(paths.DATA_JSON, encoding="utf-8-sig") as f:
            return (json.load(f).get("accounts") or {})
    except Exception:
        return {}


def fmt_ts(ts):
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "-"


# ---------------------------------------------------------------------------

def cmd_list(args):
    d = W.load_watch()
    st = W.load_state()
    au = d.get("authors") or []
    print("名单：%d 位作者   （文件：%s）\n" % (len(au), W.WATCH_FILE))
    if not au:
        print("还是空的。用 --add-top 50 从本地库挑作品最多的，或 --add 加指定作者。")
        return 0
    for i, a in enumerate(au, 1):
        s = st["authors"].get(str(a.get("uid"))) or {}
        flag = "" if a.get("enabled", True) else "  [已停用]"
        print("%3d. %-18s uid=%-18s 已下 %-4s 上次查 %-16s 下轮 %s%s"
              % (i, (a.get("nickname") or "?")[:18], a.get("uid"),
                 s.get("downloads", 0), s.get("last_scan") or "-",
                 fmt_ts(s.get("next_check") or 0) if s.get("next_check") else "-",
                 flag))
    return 0


def cmd_status(args):
    d = W.load_watch()
    st = W.load_state()
    cfg = d["settings"]
    au = d.get("authors") or []
    cnt = {"hot": 0, "warm": 0, "cold": 0, "idle": 0}
    for a in au:
        s = st["authors"].get(str(a.get("uid"))) or {}
        t = s.get("tier") or ("hot" if s.get("is_hot") else "idle")
        cnt[t] = cnt.get(t, 0) + 1
    total_dl = sum(int((st["authors"].get(str(a.get("uid"))) or {}).get("downloads") or 0)
                   for a in au)
    print("名单 %d 位   热 %d / 温 %d / 冷 %d / 静 %d   累计追更下载 %d 条"
          % (len(au), cnt["hot"], cnt["warm"], cnt["cold"], cnt["idle"], total_dl))
    print("节奏：热 %.0f 分钟 / 温 %.0f 分钟 / 冷 %.0f 小时 / 静 %.0f 小时 / 每个作者间隔 %.1f 秒"
          % (cfg["interval_hot"] / 60.0, cfg["interval_warm"] / 60.0,
             cfg["interval_cold"] / 3600.0, cfg["interval_idle"] / 3600.0, cfg["gap"]))
    day = time.strftime("%Y-%m-%d")
    used = int(st.get("req_today") or 0) if st.get("req_day") == day else 0
    try:
        accs = W.pool_summary()
    except Exception:
        accs = []
    n_acc = len([a for a in accs if a.get("enabled")]) or 1
    print("账号池 %d 个：%s" % (len(accs), "、".join(
        "%s(%s)" % (a.get("label") or a.get("id"),
                    ("歇%d分" % (a["cool_left"] // 60)) if a.get("cool_left")
                    else ("可用" if a.get("logged_in") else "没登录"))
        for a in accs) or "空"))
    print("今日已用请求 %d / 上限 %d 次（单号 %d×%d 个号）   估算每天 %d 次"
          % (used, int(cfg.get("daily_budget") or 0) * n_acc,
             int(cfg.get("daily_budget") or 0), n_acc, W.estimate_daily(d, st)))
    left = max(0.0, float(st.get("cool_until") or 0) - time.time())
    if left > 0:
        print("⚠ 限流冷却中，还剩 %.0f 分钟（连续失败 %d 次触发）"
              % (left / 60.0, int(cfg.get("break_fails") or 8)))
    if os.path.exists(W.LOG_FILE):
        print("\n最近日志（%s）：" % W.LOG_FILE)
        try:
            with open(W.LOG_FILE, encoding="utf-8") as f:
                for line in f.readlines()[-15:]:
                    print("  " + line.rstrip())
        except OSError:
            pass
    return 0


def cmd_add(args):
    d = W.load_watch()
    acc = load_accounts()
    have = {str(a.get("uid")) for a in d.get("authors") or []}
    by_sec = {str(a.get("sec_uid")): (uid, a) for uid, a in acc.items() if a.get("sec_uid")}
    added = 0
    for token in args.add:
        token = str(token).strip()
        sec = norm_sec(token)
        uid, a = None, None
        if sec and sec in by_sec:
            uid, a = by_sec[sec]
        elif token.isdigit() and token in acc:
            uid, a = token, acc[token]
        elif not token.isdigit() and not sec:
            # 昵称模糊：只在本地库里找，且必须唯一，避免加错人
            hits = [(u, x) for u, x in acc.items()
                    if token.lower() in str(x.get("nickname") or "").lower()]
            if len(hits) == 1:
                uid, a = hits[0]
            elif len(hits) > 1:
                print("× 「%s」在库里匹配到 %d 位，请改用 uid：%s"
                      % (token, len(hits),
                         "、".join("%s(%s)" % (x.get("nickname"), u) for u, x in hits[:8])))
                continue
        if not uid or not a:
            print("× 认不出「%s」——库里没有这个 uid/sec_uid。先在 8723 里把它收进库，"
                  "或者直接把 uid 给我。" % token)
            continue
        if str(uid) in have:
            print("· %s 已经在名单里了" % (a.get("nickname") or uid))
            continue
        d["authors"].append({
            "uid": str(uid), "sec_uid": a.get("sec_uid") or "",
            "nickname": a.get("nickname") or "", "added_at": W.now_str(),
            "enabled": True,
            # 新加入 → 先把现有作品整体扫一遍并下载缺的（2026-11-10 用户要求）
            "full_scan": True,
        })
        have.add(str(uid))
        added += 1
        print("＋ %s (uid=%s)" % (a.get("nickname"), uid))
    if added:
        W.save_watch(d)
        print("\n已加入 %d 位。下一轮会先把 TA 的现有作品整体扫一遍，缺的补下。" % added)
    return 0


def cmd_add_top(args):
    d = W.load_watch()
    acc = load_accounts()
    have = {str(a.get("uid")) for a in d.get("authors") or []}
    rows = [(len(a.get("works") or []), uid, a) for uid, a in acc.items()
            if str(a.get("sec_uid") or "").strip() and str(uid) not in have]
    rows.sort(key=lambda x: x[0], reverse=True)
    picked = rows[:max(0, int(args.add_top))]
    for n, uid, a in picked:
        d["authors"].append({
            "uid": str(uid), "sec_uid": a.get("sec_uid") or "",
            "nickname": a.get("nickname") or "", "added_at": W.now_str(),
            "enabled": True,
            "full_scan": True,
        })
    if picked:
        W.save_watch(d)
        print("已加入 %d 位（按本地作品数排序）：" % len(picked))
        for n, uid, a in picked[:15]:
            print("   %-18s %d 个作品" % ((a.get("nickname") or "?")[:18], n))
        if len(picked) > 15:
            print("   … 还有 %d 位" % (len(picked) - 15))
        print("\n注意：这只是按「库里作品多」排的，不一定是你想追的。"
              "名单在 data/watch.json，可以手改，也可以 --remove 去掉。")
    else:
        print("没有可加的：库里作者都已经在名单里了，或者都没有 SEC_UID。")
    return 0


def cmd_remove(args):
    d = W.load_watch()
    before = len(d.get("authors") or [])
    drop = set()
    for t in args.remove:
        t = str(t).strip()
        sec = norm_sec(t)
        for a in d.get("authors") or []:
            if str(a.get("uid")) == t or (sec and a.get("sec_uid") == sec) \
                    or t.lower() == str(a.get("nickname") or "").lower():
                drop.add(str(a.get("uid")))
    if not drop:
        print("× 名单里没有匹配的")
        return 1
    d["authors"] = [a for a in d.get("authors") or [] if str(a.get("uid")) not in drop]
    W.save_watch(d)
    print("已移出 %d 位（%d → %d）。已下载的文件不受影响。"
          % (len(drop), before, len(d["authors"])))
    return 0


def _run(scan_args, dry=False, only_uid=None):
    watch = W.load_watch()
    state = W.load_state()
    if not (watch.get("authors") or []):
        print("名单是空的：先 --add-top 50 或 --add 加几位")
        return None
    if scan_args:
        watch = {"authors": scan_args, "settings": watch["settings"]}
    dy = W.Douyin()
    lib_idx, lib_folders = W.load_library_index()
    return W.scan_round(dy, watch, state, lib_idx, lib_folders,
                        dry=dry, only_uid=only_uid)


def cmd_scan(args):
    t0 = time.time()
    st = _run(None, dry=args.dry)
    if st is None:
        return 1
    el = time.time() - t0
    print("\n=== 本轮结束（%.1f 秒）===" % el)
    print("查了 %d 位  发现新作品 %d 条  下载 %d 条（%.1f MB）  跳过 %d  失败 %d"
          % (st["checked"], st["new"], st["downloaded"],
             st["bytes"] / 1048576.0, st["skipped"], st["failed"]))
    if st.get("paused"):
        print("⏸ %s" % st["paused"])
    if st.get("requests"):
        print("本轮发了 %d 次请求" % st["requests"])
    for e in (st["errors"] or [])[:8]:
        print("  ! " + e)
    if args.dry:
        print("\n这是试运行：会发现 %d 条待下载，一条都没下，状态也没动。"
              "去掉 --dry 才会真的下。" % int(st.get("would") or 0))
    return 0


def cmd_loop(args):
    """常驻：每隔一分钟看一次「谁到点了」，到点的扫一轮。

    停机（关电脑）期间什么都不做；开机重启后 next_check 还在，会立刻把
    「早就该查」的那批一次扫完 —— 这就是补漏，不需要额外逻辑。
    """
    W.log("=== 追更常驻启动（名单 %d 位）===" % len(W.load_watch().get("authors") or []))
    # 号池暂时全在冷却（比如上次被误判限流）不该让常驻进程直接退出 ——
    # 等一会儿它们就恢复了，起来之后照常接着追。
    while True:
        try:
            dy = W.Douyin()
            break
        except W.RetryLater as e:
            W.log("… 账号池暂时不可用（%s），60 秒后重试" % str(e)[:80])
            time.sleep(60)
        except Exception as e:
            W.log("× 启动失败：%s" % e)
            return 2
    # PID 落到文件里：网页那边「停止」要能精确杀掉它（server 重启后内存里的句柄就没了）
    try:
        with open(os.path.join(paths.DATA_DIR, "watch.pid"), "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    while True:
        try:
            watch = W.load_watch()
            state = W.load_state()
            # 心跳：网页靠它判断「现在到底跑没跑」（超过 3 分钟没更新就当停了）
            state["heartbeat"] = time.time()
            W.save_state(state)
            due = W.which_authors_due(watch, state)
            if not due:
                time.sleep(60)
                continue
            # 冷却中 / 额度用完：不发请求，老老实实等（等多久看闸门怎么说）
            ok, why = W.guard(state, watch["settings"])
            if not ok:
                W.save_state(state)
                W.log("⏸ %s" % why)
                left = float(state.get("cool_until") or 0) - time.time()
                time.sleep(max(30.0, min(float(left) if left > 0 else 300.0, 300.0)))
                continue
            W.log("到点 %d 位，开扫" % len(due))
            lib_idx, lib_folders = W.load_library_index()
            # 一轮可能跑十几分钟（几十上百位作者）。用户在这期间新加了作者，
            # 不能让他干等到整轮结束 —— 2026-11-10 的要求是「加进追更就立刻扫一遍」。
            # 所以把这一轮切成小块（每块 30 位），块与块之间重新读一次名单，
            # 把新加进来、还没全量过的作者插到下一块最前面。
            # 下载额度仍是「整轮共用 max_per_round」：每块按剩余额度递减，
            # 不然切块会把「单轮最多 40 条」的防爆量限制放大成 40×块数。
            chunk_size = 30
            left = int(watch["settings"].get("max_per_round") or 40)
            queue = list(due)
            res = {}
            while queue and left > 0:
                block, queue = queue[:chunk_size], queue[chunk_size:]
                try:
                    fresh = W.which_authors_due(W.load_watch(), state)
                except Exception:
                    fresh = []
                for x in fresh:
                    u = str(x.get("uid") or "")
                    st_x = state["authors"].get(u) or {}
                    if not u or not x.get("full_scan"):
                        continue
                    if st_x.get("full_done_for") == (x.get("added_at") or ""):
                        continue
                    # 已经在本轮排过的就不重复排
                    if any(str(y.get("uid")) == u for y in block + queue):
                        continue
                    queue.insert(0, x)
                    W.log("＋ 名单里新加了一位，插到本块前面先扫")
                cfg2 = dict(watch["settings"])
                cfg2["max_per_round"] = max(1, left)
                res = W.scan_round(dy, {"authors": block, "settings": cfg2},
                                   state, lib_idx, lib_folders) or {}
                W.save_state(state)
                left -= int(res.get("downloaded") or 0)
                if res.get("paused"):
                    break
            if res.get("paused"):
                time.sleep(60)
        except KeyboardInterrupt:
            W.log("已手动停止")
            return 0
        except Exception as e:
            W.log("× 循环里出错：%s: %s（60 秒后继续）" % (type(e).__name__, e))
            time.sleep(60)


def main():
    ap = argparse.ArgumentParser(description="追更：盯着重点作者，有新作品就下载")
    ap.add_argument("--list", action="store_true", help="看名单")
    ap.add_argument("--status", action="store_true", help="看状态和最近日志")
    ap.add_argument("--add", nargs="+", help="加作者：uid / sec_uid / 主页链接 / 库里的昵称")
    ap.add_argument("--add-top", type=int, help="从本地库挑作品最多的 N 位加入")
    ap.add_argument("--remove", nargs="+", help="移出名单")
    ap.add_argument("--scan", action="store_true", help="扫一轮")
    ap.add_argument("--dry", action="store_true", help="配合 --scan：只说不做")
    ap.add_argument("--loop", action="store_true", help="常驻运行")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if args.list:
        return cmd_list(args)
    if args.status:
        return cmd_status(args)
    if args.add:
        return cmd_add(args)
    if args.add_top:
        return cmd_add_top(args)
    if args.remove:
        return cmd_remove(args)
    if args.scan:
        return cmd_scan(args)
    if args.loop:
        return cmd_loop(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
