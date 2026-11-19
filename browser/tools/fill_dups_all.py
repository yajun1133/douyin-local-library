# -*- coding: utf-8 -*-
"""按追更名单批量补「同一天多条同名作品」（--fill-dups 的批量版）。

为什么需要它：文件名是 `日期-昵称-描述`，同一天两条同描述的作品会算出同一个名字，
DouK 当年只留下最后一条；追更的第 3 层判重也照这个错误假设跳过。于是「抖音 76 条、
本地 64 条」这种缺口会静悄悄存在。`backfill_author.py --fill-dups` 能补一位作者，
这里是按名单跑、可中断续跑的外壳。

排除口径（2026-11-13 用户要求「只跑追更的，不好看了 / 卖号 / 注销 的不用」）：
  · 不在追更名单（名单就是 watch.json，天然满足「只跑追更的」）
  · 停用（enabled=false，基本是扫不到/私密）
  · 标注 status ∈ {不好看了, 卖号, 注销, 私密账号}
  · 没有 sec_uid、本地没有文件夹（下了也没地方放）

进度写在 data/fill_dups_progress.json，中断后重跑同一条命令即可接着跑。
日志 data/fill_dups.log。

用法：
    python tools/fill_dups_all.py                # 全量（按上面的排除规则）
    python tools/fill_dups_all.py --only 20      # 只跑前 20 位（试水）
    python tools/fill_dups_all.py --pages 10     # 每位最多翻几页（每页约 20 条）
    python tools/fill_dups_all.py --reset        # 清掉进度重头跑
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

import paths                     # noqa: E402
import watch_core as W           # noqa: E402
import backfill_author as B      # noqa: E402

PROG = os.path.join(paths.DATA_DIR, "fill_dups_progress.json")
LOG = os.path.join(paths.DATA_DIR, "fill_dups.log")

SKIP_STATUS = {"不好看了", "卖号", "注销", "私密账号"}


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(p, default):
    try:
        with open(p, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def pick_authors(pages_note=""):
    """按排除规则挑出要跑的作者，返回 [(uid, sec, nick, status), ...]。"""
    watch = load_json(os.path.join(paths.DATA_DIR, "watch.json"), {})
    ud = (load_json(os.path.join(paths.DATA_DIR, "user_data.json"), {})
          .get("accounts") or {})
    lib = load_json(os.path.join(paths.DATA_DIR, "data.json"), {}).get("accounts") or {}

    out, skipped = [], {}
    for a in watch.get("authors") or []:
        uid = str(a.get("uid") or "")
        if not uid:
            continue
        st = (ud.get(uid) or {}).get("status") or ""
        if not a.get("enabled", True):
            skipped["停用"] = skipped.get("停用", 0) + 1
            continue
        if st in SKIP_STATUS:
            skipped[st] = skipped.get(st, 0) + 1
            continue
        sec = str(a.get("sec_uid") or "").strip()
        if not sec or uid not in lib:
            skipped["无 sec_uid / 无本地文件夹"] = \
                skipped.get("无 sec_uid / 无本地文件夹", 0) + 1
            continue
        out.append((uid, sec, a.get("nickname") or (lib[uid] or {}).get("nickname") or uid, st))
    log("待跑 %d 位%s；跳过：%s" % (len(out), pages_note,
                                "、".join("%s %d" % (k, v) for k, v in skipped.items())))
    return out


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="按追更名单批量补同日同名作品")
    ap.add_argument("--pages", type=int, default=10, help="每位最多翻几页（默认 10 ≈ 200 条）")
    ap.add_argument("--only", type=int, default=0, help="只跑前 N 位（试水用）")
    ap.add_argument("--reset", action="store_true", help="清掉进度，从头跑")
    args = ap.parse_args()

    if args.reset and os.path.exists(PROG):
        os.remove(PROG)
        log("进度已清空，从头跑")

    prog = load_json(PROG, {"done": {}, "totals": [0, 0, 0, 0], "started": ""})
    if not prog.get("started"):
        prog["started"] = time.strftime("%Y-%m-%d %H:%M:%S")
    done = prog.get("done") or {}

    todo = pick_authors()
    if args.only:
        todo = todo[:args.only]
    todo = [x for x in todo if x[0] not in done]
    if not todo:
        log("没有要跑的作者（都跑过了；要重跑加 --reset）")
        return 0

    dy = W.Douyin()
    lib_idx, lib_folders = W.load_library_index()
    douk_ids = W.douk_downloaded_ids()
    log("DouK 下载记录 %d 条；本地索引 %d 位作者" % (len(douk_ids), len(lib_idx)))

    t0 = time.time()
    for i, (uid, sec, nick, st) in enumerate(todo, 1):
        # 磁盘保护：E 盘不是无限的，缺的作品一多就是几十 GB。低于 15 GB 就停，
        # 进度已经落盘，腾出空间后重跑同一条命令接着跑。
        try:
            import shutil as _sh
            _free = _sh.disk_usage(paths.SCAN_ROOT).free / 1024 ** 3
            if _free < 15:
                log("⚠ 剩余空间只剩 %.1f GB，先停下（进度已保存，腾出空间后重跑即可继续）" % _free)
                return 3
        except OSError:
            pass
        log("—— [%d/%d] %s（%s）uid=%s" % (i, len(todo), nick, st or "未标", uid))
        try:
            r = B.backfill_one(dy, lib_idx, lib_folders, uid, sec, nick,
                               args.pages, douk_ids, fill_dups=True)
        except Exception as e:
            log("× %s 出异常：%s: %s" % (nick, type(e).__name__, e))
            r = (0, 0, 1, 0)
        tot = prog.get("totals") or [0, 0, 0, 0]
        prog["totals"] = [x + y for x, y in zip(tot, r)]
        done[uid] = {"nick": nick, "r": list(r), "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
        prog["done"] = done
        try:
            with open(PROG, "w", encoding="utf-8") as f:
                json.dump(prog, f, ensure_ascii=False, indent=1)
        except OSError:
            pass
        # 新下的文件要立刻进索引，否则同一位作者下一轮判重还会当成"没有"
        if r[0]:
            try:
                lib_idx, lib_folders = W.load_library_index()
            except Exception:
                pass
        el = time.time() - t0
        avg = el / i
        log("   小计 新增 %d / 跳过 %d / 失败 %d / 已删不补 %d ｜ 累计新增 %d ｜ "
            "还剩约 %.0f 分钟" % (r[0], r[1], r[2], r[3], prog["totals"][0],
                              avg * (len(todo) - i) / 60.0))

    log("=" * 60)
    log("全部完成：新增 %d 条  跳过 %d 条  失败 %d 条  已删不补 %d 条（共 %d 位作者）"
        % (prog["totals"][0], prog["totals"][1], prog["totals"][2],
           prog["totals"][3], len(done)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
