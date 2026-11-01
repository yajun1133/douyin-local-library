#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一处升版本号，两个文件同步。

以前只改 index.html 的 ?v= 缓存参数，右下角那个角标（写在 style.css 的
content: 'vXxx' 里）就一直是旧的，看着像"没更新"，白排查半天。

用法：
    python tools/bump_version.py              # 自动 +1（同一天内递增）
    python tools/bump_version.py --show       # 只看当前版本
    python tools/bump_version.py --to c102    # 指定尾号
    python tools/bump_version.py --to 20261030c102   # 指定整串
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "src", "web")
INDEX = os.path.join(WEB, "index.html")
CSS = os.path.join(WEB, "style.css")

PAT = re.compile(r"\d{8}c\d+")


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)


def current():
    """返回 (index.html 里的版本, style.css 角标里的版本)；取数字最大的那个当基准。"""
    idx = sorted(set(PAT.findall(read(INDEX))))
    css = sorted(set(PAT.findall(read(CSS))))
    return idx, css


def bump(target):
    idx, css = current()
    olds = set(idx) | set(css)
    if not olds:
        print("[x] 两个文件里都没找到版本号，检查 index.html 的 ?v= 和 style.css 的 content")
        return 1
    if target is None:
        # 取序号最大的做基准，然后 +1
        base = max(olds, key=lambda v: int(v.split("c")[1]))
        day, seq = base.split("c")
        target = "%sc%d" % (day, int(seq) + 1)

    n1 = n2 = 0
    s = read(INDEX)
    for o in olds:
        n1 += s.count(o)
        s = s.replace(o, target)
    write(INDEX, s)

    s = read(CSS)
    for o in olds:
        n2 += s.count(o)
        s = s.replace(o, target)
    write(CSS, s)

    print("旧版本: index=%s  css=%s" % (idx or "-", css or "-"))
    print("新版本: %s" % target)
    print("index.html 替换 %d 处 | style.css 替换 %d 处" % (n1, n2))
    if n1 == 0:
        print("[!] index.html 没替换到，确认里面还有 ?v=<版本>")
    if n2 == 0:
        print("[!] style.css 没替换到，确认 body::after 的 content 还在")
    print("\n浏览器按 Ctrl+F5 强刷，右下角角标应该变成 v%s" % target)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="只显示当前版本")
    ap.add_argument("--to", default=None, help="指定新版本，如 c102 或 20261030c102")
    a = ap.parse_args()

    if a.show:
        idx, css = current()
        print("index.html: %s" % (", ".join(idx) or "未找到"))
        print("style.css : %s" % (", ".join(css) or "未找到"))
        if idx and css and set(idx) != set(css):
            print("[!] 两边不一致 —— 右上角/右下角显示会对不上，跑一次不带参数的即可同步")
        return 0

    target = a.to
    if target:
        if re.fullmatch(r"c\d+", target):
            idx, css = current()
            base = max(set(idx) | set(css), key=lambda v: int(v.split("c")[1]))
            target = base.split("c")[0] + target
        if not PAT.fullmatch(target):
            print("[x] 版本号格式应为 20261030c102 或 c102")
            return 2
    return bump(target)


if __name__ == "__main__":
    sys.exit(main())
