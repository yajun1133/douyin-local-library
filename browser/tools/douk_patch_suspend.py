#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
改 DouK 下载器那两个「写死」的等待参数（界面上、settings.json 里都没有）。

参数在哪（都是官方源码里写死的常量，编译进了 pyc）：
    lib/src/custom/function.pyc
    ├─ suspend(count, console)          批量模式每处理 count % 10 == 0 个账号 → 暂停 300 秒
    └─ get_wait_time(avg_delay=6.0, sigma=0.5)
                                        抓数据时每个网络请求之间随机等一会儿（对数正态，
                                        夹在 1.5 ~ 33.3 秒，平均约 6 秒）；不影响下载文件本身

为什么能改：
    lib/src 下只有 .pyc 没有 .py，属于 sourceless import，Python 只校验文件头 magic，
    不校验源码。所以把 code object 里的常量换掉、重新 marshal 写回就行，不用重新打包。

用法（在 browser/ 目录下跑）：
    python tools/douk_patch_suspend.py --show                 # 只看现在的值
    python tools/douk_patch_suspend.py --every 20 --rest 60   # 每 20 个账号停 60 秒
    python tools/douk_patch_suspend.py --off                  # 关掉批量暂停
    python tools/douk_patch_suspend.py --delay 3              # 请求平均间隔改成 3 秒
    python tools/douk_patch_suspend.py --restore              # 还原官方原版

改之前会把原始 pyc 备份到 backup/douk_function_原版.pyc；改完必须**重启 DouK** 才生效。
注意：DouK 升级/重装会把这个文件覆盖回官方值，到时重跑一次本脚本即可。
"""

import argparse
import marshal
import os
import shutil
import subprocess
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))
import paths  # noqa: E402

TARGET = os.path.join(paths.DOWNLOADER_DIR, "lib", "src", "custom", "function.pyc")
BACKUP = os.path.join(paths.BACKUP_DIR, "douk_function_原版.pyc")

ORIG_EVERY = 10      # 官方：每 10 个账号
ORIG_REST = 300      # 官方：暂停 300 秒（5 分钟）
ORIG_DELAY = (6.0, 0.5)
OFF_EVERY = 1_000_000_000    # 「关闭」的做法：批次拉到 10 亿，永远轮不到暂停


def need_py312():
    """code object 跟 Python 版本绑死，必须用 3.12 读写这份 pyc。"""
    if sys.version_info[:2] == (3, 12):
        return
    exe = getattr(paths, "PYTHON312", r"C:\Python312\python.exe")
    if not os.path.exists(exe):
        raise SystemExit("这份 pyc 是 Python 3.12 编的，需要 3.12 解释器：%s 不存在" % exe)
    sys.exit(subprocess.call([exe, os.path.abspath(__file__)] + sys.argv[1:]))


def load(path=TARGET):
    with open(path, "rb") as f:
        head = f.read(16)
        return head, marshal.load(f)


def dump(path, head, code):
    tmp = path + ".new"
    with open(tmp, "wb") as f:
        f.write(head)
        marshal.dump(code, f)
    os.replace(tmp, path)


def walk(co):
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from walk(c)


def show():
    head, top = load()
    sus = next(c for c in walk(top) if c.co_name == "suspend")
    every = next(c for c in sus.co_consts if isinstance(c, int) and not isinstance(c, bool))
    rest = [c for c in sus.co_consts if isinstance(c, int) and not isinstance(c, bool)][1]
    delay = next((c for c in top.co_consts
                  if isinstance(c, tuple) and len(c) == 2 and all(isinstance(x, float) for x in c)), None)
    print("配置文件 : %s" % TARGET)
    print("批量暂停 : 每 %s 个账号 → 暂停 %s 秒%s"
          % (every, rest, "（已关闭）" if every >= OFF_EVERY else ""))
    print("请求间隔 : 平均 %s 秒（对数正态，实际 1.5 ~ 33.3 秒）" % (delay[0] if delay else "?"))
    print("原始备份 : %s%s" % (BACKUP, "" if os.path.exists(BACKUP) else "（还没备份过）"))


def patch(every=None, rest=None, delay=None):
    head, top = load()

    def fix(co):
        consts = [fix(c) if isinstance(c, types.CodeType) else c for c in co.co_consts]
        if co.co_name == "suspend":
            ints = [i for i, c in enumerate(consts) if isinstance(c, int) and not isinstance(c, bool)]
            if every is not None:
                consts[ints[0]] = every
            if rest is not None:
                consts[ints[1]] = rest
        if co.co_name == "<module>" and delay is not None:
            consts = [(delay, 0.5) if (isinstance(c, tuple) and c == ORIG_DELAY) else c
                      for c in consts]
        return co.replace(co_consts=tuple(consts))

    new = fix(top)
    if not os.path.exists(BACKUP):
        os.makedirs(paths.BACKUP_DIR, exist_ok=True)
        shutil.copy2(TARGET, BACKUP)
        print("已备份官方原版: %s" % BACKUP)
    dump(TARGET, head, new)

    # 立刻验一遍：能 marshal 回来、常量是新值，才算改成功
    _, check = load()
    sus = next(c for c in walk(check) if c.co_name == "suspend")
    ints = [c for c in sus.co_consts if isinstance(c, int) and not isinstance(c, bool)]
    print("写入完成，校验：每 %s 个账号 → 暂停 %s 秒" % (ints[0], ints[1]))
    print("重启 DouK 后生效。")


def restore():
    if not os.path.exists(BACKUP):
        raise SystemExit("没有备份（%s），无法还原" % BACKUP)
    shutil.copy2(BACKUP, TARGET)
    print("已还原官方原版: %s" % TARGET)


def main():
    need_py312()
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--every", type=int, help="每处理多少个账号暂停一次")
    ap.add_argument("--rest", type=int, help="每次暂停多少秒")
    ap.add_argument("--off", action="store_true", help="关掉批量暂停")
    ap.add_argument("--delay", type=float, help="请求平均间隔（秒），官方 6")
    ap.add_argument("--restore", action="store_true")
    a = ap.parse_args()

    if a.restore:
        restore()
        show()
        return
    if a.off:
        patch(every=OFF_EVERY, rest=0)
        show()
        return
    if a.every is None and a.rest is None and a.delay is None:
        show()
        return
    if a.every is not None and a.every < 1:
        raise SystemExit("--every 至少 1（要关闭请用 --off）")
    if a.rest is not None and a.rest < 0:
        raise SystemExit("--rest 不能是负数")
    patch(every=a.every, rest=a.rest, delay=a.delay)
    show()


if __name__ == "__main__":
    main()
