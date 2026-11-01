#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 user_data.json 里的大小号关系迁移成「组内全连」（同一组所有账号互相关联）。

背景
----
老逻辑是「双向」：关联 A 和 B 时，只往 A、B 各自的 alt_accounts 里写对方。
于是 A 依次关联 B、C、D 之后，B 只知道 A，不知道 C、D —— 关系是散的。

新模型是「组」：
    accounts[uid]["alt_accounts"] = [同组其他所有人的 uid]（不含自己）
    x ∈ alt[y] ⇔ y ∈ alt[x]，且同组任意两人看到的其他成员集合完全相同
    组大小 = alt_accounts 长度 + 1

这个脚本用并查集把老数据里的边求连通分量，再把每个分量重写成团（clique）。
幂等，可以反复跑。

用法
----
    python fix_alt_groups.py --dry      # 只看会改什么，不写盘
    python fix_alt_groups.py            # 实际迁移（自动备份）
    python fix_alt_groups.py --show     # 打印所有多人组
"""
import json
import os
import shutil
import sys
from collections import Counter
from datetime import datetime

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
import sys  # noqa: E402
sys.path.insert(0, _TOOLS)                       # tools/ 下的 blacklist、downloaded
sys.path.insert(0, os.path.join(_ROOT, "src"))   # src/paths.py 统一路径
import paths  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # 仅用于拼本目录内的东西
USER_DATA = paths.USER_DATA


def load(path):
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def build_groups(accounts):
    """并查集求连通分量，返回 {root: [uid, ...]}。"""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for uid, ua in accounts.items():
        find(uid)
        if not isinstance(ua, dict):
            continue
        arr = ua.get("alt_accounts")
        if not isinstance(arr, list):
            continue
        for v in arr:
            if isinstance(v, str) and v and v != uid:
                union(uid, v)

    groups = {}
    for u in list(parent.keys()):
        groups.setdefault(find(u), []).append(u)
    return groups


def normalize(accounts):
    """把 accounts 里的 alt_accounts 归一化成「组内全连」，返回被修改的条目数。

    与 server.py 的 normalize_alt_groups()、app.js 的 normalizeAltGroups() 同一套算法：
    并查集求连通分量 → 每个分量重写成团。幂等，可反复调用。
    """
    groups = build_groups(accounts)
    changed = 0
    for g in groups.values():
        if len(g) < 2:
            ua = accounts.get(g[0])
            if isinstance(ua, dict) and isinstance(ua.get("alt_accounts"), list) and not ua["alt_accounts"]:
                ua.pop("alt_accounts", None)
                changed += 1
            continue
        s = sorted(set(g))
        for u in s:
            others = [x for x in s if x != u]
            entry = accounts.get(u)
            if not isinstance(entry, dict):
                entry = accounts[u] = {}
            cur = entry.get("alt_accounts")
            if not isinstance(cur, list) or sorted(set(cur)) != others or len(cur) != len(set(cur)):
                entry["alt_accounts"] = others
                changed += 1
    return changed


def main():
    dry = "--dry" in sys.argv
    show = "--show" in sys.argv

    if not os.path.isfile(USER_DATA):
        print("找不到 %s" % USER_DATA)
        return 1

    data = load(USER_DATA)
    accounts = data.get("accounts") or {}
    groups = build_groups(accounts)
    multi = [sorted(g) for g in groups.values() if len(g) > 1]

    before_sizes = Counter(len(g) for g in groups.values())
    nodes = len(set(accounts.keys()) | {v for ua in accounts.values() if isinstance(ua, dict)
                                         for v in (ua.get("alt_accounts") or []) if isinstance(v, str) and v})
    print("读入 %s" % USER_DATA)
    print("  条目 %d ｜ 节点 %d ｜ 分量 %d ｜ 多人组 %d" %
          (len(accounts), nodes, len(groups), len(multi)))
    print("  组大小分布（迁移前）: %s" % dict(sorted(before_sizes.items())))

    # 统计违反团不变量的条目
    bad = []
    for g in multi:
        s = set(g)
        for u in g:
            cur = accounts.get(u, {}).get("alt_accounts") or []
            if set(cur) != (s - {u}):
                bad.append(u)
    print("  违反「组内全连」的成员条目: %d" % len(bad))

    if show:
        print("\n所有多人组（每组一行）:")
        for g in sorted(multi, key=lambda x: (-len(x), x[0])):
            names = []
            for u in g:
                nick = (accounts.get(u) or {}).get("nickname") or ""
                names.append("%s%s" % (u, "(%s)" % nick if nick else ""))
            print("  [%d] %s" % (len(g), "  ".join(names)))
        print("")

    changed = normalize(accounts)

    print("  需要修改的条目: %d" % changed)

    if dry:
        print("\n--dry：未写盘。去掉 --dry 即可执行迁移。")
        return 0
    if not changed:
        print("\n已经是最新结构，无需改动。")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    import os as _os
    _bak_dir = paths.BACKUP_DIR
    _os.makedirs(_bak_dir, exist_ok=True)
    backup = _os.path.join(_bak_dir, _os.path.basename(USER_DATA) + ".bak-" + stamp)
    shutil.copy2(USER_DATA, backup)
    # 原子替换写盘，防止中途崩溃留下半截 JSON
    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(dir=HERE, prefix=".user_data.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, USER_DATA)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    after = build_groups(accounts)
    after_multi = [g for g in after.values() if len(g) > 1]
    links = sum(len((accounts.get(u) or {}).get("alt_accounts") or []) for u in accounts)
    edges_before = 2 * sum(len(g) * (len(g) - 1) // 2 for g in groups.values() if len(g) > 1)
    edges_after = 2 * sum(len(g) * (len(g) - 1) // 2 for g in after_multi)
    print("\n已写入 %s" % USER_DATA)
    print("  备份 %s" % os.path.basename(backup))
    print("  多人组 %d 个 ｜ 每条 alt 条目数合计 %d（迁移前按二元边计约 %d）" %
          (len(after_multi), links, edges_before))
    print("  组大小分布（迁移后）: %s" % dict(sorted(Counter(len(g) for g in after.values()).items())))
    print("  全连后应有条目数 = Σ组内人数 = %d" % edges_after)
    print("  一致" if links == edges_after else "  不一致，请检查")
    return 0


if __name__ == "__main__":
    sys.exit(main())
