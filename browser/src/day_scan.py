#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫 day 档：把「更新频率 = day」的作者整体扫一遍，有新作品就下。

    C:\\Python312\\python.exe src\\day_scan.py --run      # 跑一次（前台，跑完退出）
    C:\\Python312\\python.exe src\\day_scan.py --plan     # 只列出会扫哪些人，不联网

为什么单独一个脚本，而不是塞进 watch.py：
  抖音签名库（curl_cffi / ABogus）只有 3.12 编译版，联网下文件这件事只能像
  watch.py 一样用 C:\\Python312\\python.exe 跑；server 那边只负责起停和读状态。

和追更的三处区别：
  ① 名单来源不是 watch.json，而是 user_data 里 update_freq == "day" 再减掉追更名单
     （用户 2026-11-09 明确要求：下载 day 的时候要剔除追更的用户，免得同一批人查两遍）；
  ② 状态单独存 day_scan_state.json —— 不碰追更的基线/节奏，两边互不干扰；
  ③ 下载明细带 src="day"，界面上能单独列出来。

扫描/判重/命名全部复用 watch_core.scan_round，跟追更是同一套口径，不另起炉灶。
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths          # noqa: E402
import watch_core as W  # noqa: E402

DAY_STATE_FILE = os.path.join(paths.DATA_DIR, "day_scan_state.json")
PROGRESS_FILE = os.path.join(paths.DATA_DIR, "day_scan.json")
LOG_FILE = os.path.join(paths.DATA_DIR, "day_scan.log")
PID_FILE = os.path.join(paths.DATA_DIR, "day_scan.pid")

BATCH_TOTAL_BUDGET = 400      # 这一次最多下多少条（防一口气爆量）；可在 settings 里覆盖
MAX_ERRORS_KEEP = 20


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError:
        pass


def _read(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        log("× 写 %s 失败：%s" % (os.path.basename(path), e))


def load_state():
    d = _read(DAY_STATE_FILE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("authors", {})
    d.setdefault("req_day", time.strftime("%Y-%m-%d"))
    d.setdefault("req_today", 0)
    d.setdefault("consec_fail", 0)
    d.setdefault("cool_until", 0)
    d.setdefault("cool_level", 0)
    return d


def save_state(d):
    _write(DAY_STATE_FILE, d)


def load_progress():
    d = _read(PROGRESS_FILE, {})
    return d if isinstance(d, dict) else {}


def save_progress(**kw):
    d = load_progress()
    d.update(kw)
    d["updated"] = time.time()
    _write(PROGRESS_FILE, d)


FREQ_CHOICES = ("day", "3天", "周", "月")


def pick_authors(freq="day"):
    """指定频率档、且有 sec_uid 和作者文件夹、且不在追更名单里。

    「更新频率」是用户标注，在 user_data.json；sec_uid / 文件夹 / 作品数
    是扫描出来的，在 data.json。两个文件按 uid 对齐。
    """
    data = _read(paths.DATA_JSON, {})
    accounts = data.get("accounts") or {}
    uacc = (_read(paths.USER_DATA, {}) or {}).get("accounts") or {}
    watch_uids = {str(a.get("uid") or "")
                  for a in (W.load_watch().get("authors") or [])}
    out, skip_watch, skip_bad = [], 0, 0
    for uid, ua in uacc.items():
        if not isinstance(ua, dict):
            continue
        if str(ua.get("update_freq") or "").strip() != freq:
            continue
        uid = str(uid)
        if uid in watch_uids:
            skip_watch += 1
            continue
        a = accounts.get(uid) or {}
        sec = str(ua.get("sec_uid") or a.get("sec_uid") or "").strip()
        folder = str(a.get("folder") or ua.get("folder") or "").strip()
        if not sec or not folder:
            skip_bad += 1
            continue
        out.append({"uid": uid, "sec_uid": sec,
                    "nickname": a.get("nickname") or ua.get("nickname") or uid})
    # 作品多的先扫：一样跑一趟，先把大户补齐更划算
    def _n(x):
        return len((accounts.get(x["uid"]) or {}).get("works") or [])
    out.sort(key=lambda x: -_n(x))
    return out, skip_watch, skip_bad


def cmd_plan(args):
    rows, sw, sb = pick_authors(args.freq)
    print("「%s」档要扫：%d 位（剔除追更 %d 位、缺 SEC_UID/文件夹 %d 位）"
          % (args.freq, len(rows), sw, sb))
    for r in rows[:30]:
        print("  ", r["nickname"], r["uid"])
    if len(rows) > 30:
        print("   …… 还有 %d 位" % (len(rows) - 30))
    return 0


def cmd_run(args):
    t0 = time.time()
    freq = args.freq
    rows, skip_watch, skip_bad = pick_authors(freq)
    if not rows:
        save_progress(running=False, total=0, done=0, downloaded=0,
                      finished_at=time.time(),
                      note="「%s」档没有要扫的作者（都在追更名单里，或没有 SEC_UID/文件夹）" % freq)
        log("没有要扫的作者")
        return 0

    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    watch = W.load_watch()
    cfg = watch.get("settings") or {}
    budget = int(cfg.get("day_budget") or BATCH_TOTAL_BUDGET)
    save_progress(running=True, freq=freq, started_at=t0, finished_at=0,
                  total=len(rows), done=0, downloaded=0, skipped=0, failed=0,
                  current="", budget=budget, errors=[], skip_watch=skip_watch,
                  skip_bad=skip_bad, note="")
    log("=== 扫「%s」档开始：%d 位（剔除追更 %d 位、缺资料 %d 位），本次下载上限 %d 条 ==="
        % (freq, len(rows), skip_watch, skip_bad, budget))

    state = load_state()
    lib_idx, lib_folders = W.load_library_index()
    total_dl = 0
    done = 0
    errors = []
    paused = ""

    try:
        dy = W.Douyin()
    except Exception as e:
        log("× 启动失败：%s: %s" % (type(e).__name__, e))
        save_progress(running=False, finished_at=time.time(),
                      note="启动失败：%s" % str(e)[:120])
        return 2

    for item in rows:
        if total_dl >= budget:
            log("· 已达本次下载上限（%d 条），剩下的下次再扫" % budget)
            paused = "达到本次下载上限 %d 条" % budget
            break
        # 闸门：额度用完 / 冷却中就停（复用追更那套判断，配置也是同一份）
        ok, why = W.guard(state, cfg)
        if not ok:
            log("⏸ %s" % why)
            paused = why
            break

        save_progress(current=item["nickname"], done=done, downloaded=total_dl)
        try:
            # save_fn 必须传自己的：不传的话 scan_round 会把状态写进追更的
            # watch_state.json，把 268 位名单覆盖成「这一轮扫到的几位」
            res = W.scan_round(dy, {"authors": [item], "settings": cfg},
                               state, lib_idx, lib_folders, src="day",
                               save_fn=save_state)
        except KeyboardInterrupt:
            raise
        except W.RetryLater as e:
            log("… %s 暂时查不了（%s），跳过" % (item["nickname"], str(e)[:60]))
            continue
        except Exception as e:
            log("× %s 查询失败：%s: %s" % (item["nickname"], type(e).__name__, e))
            errors.append("%s：%s" % (item["nickname"], str(e)[:80]))
            continue

        total_dl += int(res.get("downloaded") or 0)
        done += 1
        save_state(state)
        errs = res.get("errors") or []
        for e in errs:
            errors.append(e)
        save_progress(done=done, downloaded=total_dl, failed=res.get("failed") or 0,
                      errors=errors[-MAX_ERRORS_KEEP:])
        if errs:
            log("%s：%s" % (item["nickname"], errs[0][:80]))
        # 和追更一样的节奏：别让请求曲线是一条直线
        gap = float(cfg.get("gap") or 2.0)
        jitter = float(cfg.get("gap_jitter") or 0.3)
        time.sleep(max(0.5, gap + (jitter * (0.5 - random.random()))))

    save_state(state)
    el = time.time() - t0
    log("=== 扫「%s」档结束：扫了 %d/%d 位，下载 %d 条，用时 %.0f 秒 ==="
        % (freq, done, len(rows), total_dl, el))
    save_progress(running=False, finished_at=time.time(), done=done,
                  downloaded=total_dl, current="", errors=errors[-MAX_ERRORS_KEEP:],
                  note=paused or ("完成：扫了 %d 位，下到 %d 条" % (done, total_dl)))
    try:
        os.remove(PID_FILE)
    except OSError:
        pass
    return 0


def main():
    ap = argparse.ArgumentParser(description="把某一档更新频率的作者整体扫一遍")
    ap.add_argument("--run", action="store_true", help="跑一次")
    ap.add_argument("--plan", action="store_true", help="只列名单，不联网")
    ap.add_argument("--freq", default="day", choices=list(FREQ_CHOICES),
                    help="扫哪一档：day / 3天 / 周 / 月（默认 day）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if args.plan:
        return cmd_plan(args)
    if args.run:
        return cmd_run(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
