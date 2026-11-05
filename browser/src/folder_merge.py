# -*- coding: utf-8 -*-
"""同 uid 多文件夹自动归一。

**为什么会有多个文件夹**：下载器给作者建目录用的名字是 `UID{uid}_{当前昵称}_发布作品`。
昵称一改（最常见是加了 emoji，如 `凳子yiii` → `凳子yiii🪑`），下次下载就按新名字另建一个，
旧的留在原地。到 2026-11-05 全库已经积了 89 个这样的号：作品被拆在两处，
界面上只显示主文件夹那一半，另一半像是「没下载过」。

**为什么不改下载器**：DouK 是打包好的 exe（`library.zip` 里只有 pyc），
改不了它的目录命名。所以反过来做 —— 下载器随便建，扫描前统一收编。

**归一规则**（确定性，不依赖执行顺序）：
  1. 主文件夹 = 文件数最多的那个
  2. 文件数并列 → 优先 data.json 里记着的 `folder`（界面上正显示的那个）
  3. 还并列 → 名字字典序最小
  其余文件夹的文件逐个 `os.rename` 搬进主文件夹。

**安全底线**（这套流程会动真文件，任何一条都不能省）：
  - 同名文件先比大小：大小相同当成同一份，跳过；**大小不同绝不覆盖**，
    整个 uid 跳过并记进报告，留给人工判断
  - 只用 `os.rename`（同盘瞬时完成，不复制不中断）
  - 目录**空了才** `os.rmdir`，非空一律不动
  - 全程不用 `shutil.rmtree` / `remove`

**调用点**：
  - `scanner.py` 每次扫描开头（自动，可设环境变量 `SKIP_FOLDER_MERGE=1` 跳过）
  - `/api/folders/merge`（手动，`{"apply": false}` 时只出报告不动文件）
"""

import errno
import json
import os
import re
import sys

import download_db

UID_FOLDER_RE = re.compile(r"^UID(\d+)_(.*)_发布作品$")

# 一个文件夹里最多数多少个文件（作者目录是平铺的，几千个也就几毫秒，这里只是防异常）
COUNT_LIMIT = 200000


def _log(msg):
    try:
        sys.stdout.write(str(msg) + "\n")
        sys.stdout.flush()
    except (UnicodeEncodeError, ValueError):
        pass


def _count_files(path):
    """数文件夹里的一级文件（不递归）。读不出来返回 -1，调用方跳过这个目录。"""
    n = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False):
                        n += 1
                except OSError:
                    continue
                if n >= COUNT_LIMIT:
                    break
    except OSError:
        return -1
    return n


def _load_preferred(root):
    """data.json 里每个 uid 当前记着的 folder（用来在文件数并列时当裁判）。"""
    out = {}
    try:
        import paths
        with open(paths.DATA_JSON, "r", encoding="utf-8") as fp:
            d = json.load(fp)
        for uid, a in ((d or {}).get("accounts") or {}).items():
            if isinstance(a, dict) and a.get("folder"):
                out[str(uid)] = a["folder"]
    except Exception:
        pass
    return out


def find_groups(root):
    """扫一遍根目录，返回 {uid: [文件夹名, ...]}，只保留有 2 个及以上的。"""
    groups = {}
    try:
        with os.scandir(root) as it:
            for e in it:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                m = UID_FOLDER_RE.match(e.name)
                if not m:
                    continue
                groups.setdefault(m.group(1), []).append(e.name)
    except OSError as e:
        _log("[merge] 读不了根目录 %s: %s" % (root, e))
        return {}
    return {u: fs for u, fs in groups.items() if len(fs) > 1}


def pick_main(uid, folders, root, preferred):
    """按文件数最多 → data.json 里的 folder → 字典序最小，挑出主文件夹。"""
    counts = {}
    for f in folders:
        n = _count_files(os.path.join(root, f))
        if n >= 0:
            counts[f] = n
    if not counts:                       # 全都读不出来
        return None, {}
    pref = preferred.get(uid)
    best = max(counts.values())
    tied = sorted(f for f, n in counts.items() if n == best)
    if pref in tied:
        main = pref
    elif pref and pref in counts:
        # data.json 指的那个文件数不是最多：仍然以文件最多的为准，
        # 否则要为一次合并搬几千个文件（同盘 rename 虽快，但没必要）
        main = tied[0]
    else:
        main = tied[0]
    return main, counts


def _md5(path, limit=None):
    """算文件指纹。limit 只算前 N 字节（大文件预筛用）。"""
    import hashlib
    h = hashlib.md5()
    try:
        with open(path, "rb") as fp:
            left = limit
            while True:
                chunk = fp.read(1 << 20 if not left else min(1 << 20, left))
                if not chunk:
                    break
                h.update(chunk)
                if left:
                    left -= len(chunk)
                    if left <= 0:
                        break
    except OSError:
        return ""
    return h.hexdigest()


def _same_content(a, b):
    """同一个文件名，两份文件是不是同一份内容：先比大小，再比 MD5。"""
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
    except OSError:
        return False
    da, db = _md5(a), _md5(b)
    return bool(da) and da == db


def _precheck_conflicts(src_dirs, dst_dir):
    """先看一遍同一个 uid 的目录之间有没有「同名但不是同一份内容」的文件。

    有的话整个 uid 都不动 —— 部分合并会留下「一半在这边一半在那边」的烂摊子，
    比不合并更难收拾。同名 + 大小相同 + MD5 相同才算同一份（可以安全去重）。
    """
    bad = []
    for src_dir in src_dirs:
        try:
            names = os.listdir(src_dir)
        except OSError:
            continue
        for name in names:
            s = os.path.join(src_dir, name)
            d = os.path.join(dst_dir, name)
            try:
                if not (os.path.isfile(s) and os.path.isfile(d)):
                    continue
                if not _same_content(s, d):
                    bad.append(name)
            except OSError:
                bad.append(name)
    return bad


def _merge_files(src_dir, dst_dir, main_name, child_name, db, apply, dup_ok):
    """把 src 里的文件搬进 dst。返回 (moved, dup, conflict, failed)。"""
    moved = dup = 0
    conflict = []
    failed = []
    try:
        entries = sorted(os.listdir(src_dir))
    except OSError as e:
        return 0, 0, [], [("*目录*", str(e))]

    for name in entries:
        s = os.path.join(src_dir, name)
        d = os.path.join(dst_dir, name)
        try:
            is_file = os.path.isfile(s)
        except OSError:
            is_file = False
        if not is_file:
            # 子目录（用户自己归档的 `_旧格式` 之类）：整个搬过去，但绝不合并内容 ——
            # 同名子目录一律跳过，免得把两份不同的东西搅在一起
            if os.path.isdir(s):
                if os.path.exists(d):
                    continue
                if apply:
                    try:
                        os.rename(s, d)
                    except OSError as e:
                        failed.append((name + "/", str(e)))
            continue
        if os.path.exists(d):
            # 预检已经确认过内容一致才走到这里；重复副本要**删掉**，
            # 否则它留在原目录里，目录永远清不空、也就永远删不掉（合并等于没做）
            if not _same_content(s, d):
                conflict.append(name)
                continue
            dup += 1
            if apply:
                try:
                    os.remove(s)
                except OSError as e:
                    failed.append((name, "删重复副本失败: %s" % e))
            continue
        if not apply:
            moved += 1
            continue
        try:
            os.rename(s, d)
            moved += 1
        except OSError as e:
            failed.append((name, str(e)))

    if apply and db is not None and (moved or dup):
        # 即使一个都没搬（全是重复），也要跑一遍：那些重复副本马上随空目录消失，
        # 库里留着会让文件数虚高
        try:
            db.merge_folder(child_name, main_name, dup_ok=dup_ok)
        except Exception as e:  # noqa: BLE001
            _log("[merge] 下载记录库没同步 %s → %s: %s" % (child_name, main_name, e))
    return moved, dup, conflict, failed


def merge_all(root=None, apply=True, only_uid=None):
    """把根目录下所有「同 uid 多文件夹」合并成一个。返回报告 dict。

    apply=False 只统计会发生什么，不动磁盘 —— 先跑这个看一遍再决定。
    """
    if root is None:
        import paths
        root = paths.SCAN_ROOT
    groups = find_groups(root)
    if only_uid:
        groups = {u: fs for u, fs in groups.items() if u == only_uid}

    preferred = {}
    report = {
        "root": root, "apply": bool(apply),
        "groups": len(groups), "merged": [], "skipped": [],
        "moved": 0, "dup": 0, "conflict": 0, "failed": 0,
    }
    if not groups:
        return report
    # 只有真的有多文件夹时才读 data.json（93 MB，解析一次一两秒）——
    # auto_merge 每趟扫描都会跑，绝大多数时候是空转，不能每次都付这个成本
    preferred = _load_preferred(root)

    for uid in sorted(groups):
        folders = groups[uid]
        main, counts = pick_main(uid, folders, root, preferred)
        if not main:
            report["skipped"].append({"uid": uid, "folders": folders,
                                      "why": "每个文件夹都读不出来"})
            continue
        others = [f for f in folders if f != main]
        item = {"uid": uid, "main": main, "main_files": counts.get(main),
                "children": [], "moved": 0, "dup": 0, "conflict": 0, "failed": 0}
        blocked = False

        # 预检：有「同名不同内容」就整个 uid 跳过，不搬任何文件
        conf = _precheck_conflicts([os.path.join(root, c) for c in others],
                                   os.path.join(root, main))
        if conf:
            item["conflict"] = len(conf)
            item["blocked"] = "同名文件大小不同，整个账号原样保留，等人工看"
            item["conflictFiles"] = conf[:10]
            report["conflict"] += len(conf)
            report["skipped"].append(item)
            _log("[merge] %s: 跳过（%d 个同名不同内容的文件，如 %s）"
                 % (uid, len(conf), conf[0][:40]))
            continue

        for child in sorted(others):
            src = os.path.join(root, child)
            dst = os.path.join(root, main)
            m, d, conf, fail = _merge_files(src, dst, main, child, download_db,
                                            apply, dup_ok=True)
            item["moved"] += m
            item["dup"] += d
            item["conflict"] += len(conf)
            item["failed"] += len(fail)
            item["children"].append({"name": child, "files": counts.get(child),
                                     "moved": m, "dup": d,
                                     "conflict": conf[:5], "failed": fail[:5]})
            if conf:
                # 同名但内容不同：这个 uid 不动了，避免半合并状态
                blocked = True
            if fail:
                blocked = True
            if apply and not blocked:
                try:
                    left = os.listdir(src)
                except OSError:
                    left = ["*读不了*"]
                if not left:
                    try:
                        os.rmdir(src)
                    except OSError as e:
                        _log("[merge] 目录删除失败 %s: %s" % (child, e))
                        blocked = True

        report["moved"] += item["moved"]
        report["dup"] += item["dup"]
        report["conflict"] += item["conflict"]
        report["failed"] += item["failed"]
        if blocked:
            item["blocked"] = "有同名不同内容或搬运失败的文件，这个 uid 只完成了一部分"
            report["skipped"].append(item)
        else:
            report["merged"].append(item)
        _log("[merge] %s: %s ← %s 搬 %d 个, 判重 %d 个%s"
             % (uid, main, ",".join(others), item["moved"], item["dup"],
                "（受阻）" if blocked else ""))
    return report


def auto_merge(log=_log):
    """扫描前自动跑一次（scanner 调用）。出任何错都不能挡住扫描。"""
    if os.environ.get("SKIP_FOLDER_MERGE"):
        return None
    try:
        rep = merge_all(apply=True)
        if rep["groups"]:
            log("[merge] 同 uid 多文件夹 %d 组：合并 %d 组，搬 %d 个文件，判重 %d 个，受阻 %d 组"
                % (rep["groups"], len(rep["merged"]), rep["moved"], rep["dup"],
                   len(rep["skipped"])))
        return rep
    except Exception as e:  # noqa: BLE001
        log("[merge] 自动合并出错（不影响扫描）: %s" % e)
        return None


if __name__ == "__main__":
    dry = "--apply" not in sys.argv
    r = merge_all(apply=not dry)
    r.pop("root", None)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(json.dumps(r, ensure_ascii=False, indent=1))
