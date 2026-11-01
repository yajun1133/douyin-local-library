#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DouK 下载器参数读写（必须用 **Python 3.12** 跑，因为要读写 3.12 编译的 pyc）。

为什么单独一个文件、还要指定解释器：
    DouK 的部分参数（请求间隔、每 N 个号暂停 M 秒）不是写在配置文件里，而是编译进
    lib/src/custom/function.pyc 的常量。code object 与 Python 版本绑死，只有 3.12 能
    marshal 回来再写回去。server.py 跑在 3.13，所以用子进程调这个脚本。

命令行（server.py 就是这么调的）：
    python312 douk_config.py read           → 打印 JSON
    echo {...} | python312 douk_config.py write
                                            → 写 settings.json + 可选重打 pyc，打印 JSON

读写的白名单字段都在 SETTING_FIELDS 里，其余字段（cookie/browser_info 等）一律不动。
每次写之前把 settings.json 备份到 backup/douk_settings_<时间>.json；
第一次改 pyc 时把官方原版备份到 backup/douk_function_原版.pyc。
"""

import json
import marshal
import os
import shutil
import subprocess
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

SETTINGS = paths.DOWNLOADER_SETTINGS
FUNC_PYC = os.path.join(paths.DOWNLOADER_DIR, "lib", "src", "custom", "function.pyc")
PYC_BACKUP = os.path.join(paths.BACKUP_DIR, "douk_function_原版.pyc")
EXE_NAME = "DouK-Downloader.exe"

# 右侧面板**可改**的字段：key -> (类型, 中文名, 说明)
# 只放「纯下载行为」的参数。目录结构与文件名相关的（root / folder_name / name_format /
# folder_mode / desc_length）**故意不放进来**：
#   · root 一改，浏览器这边 paths.SCAN_ROOT 就找不到文件，整个索引全废；
#   · 浏览器的作品解析依赖「日期-昵称-描述[_序号].ext」这个格式，改了 name_format 就解析不出来；
#   · folder_name 只对「单条作品」模式生效，批量下载根本不看它（别再让人误以为有子目录）；
#   · folder_mode 不是「按作者建文件夹」——作者文件夹由 storage_folder() 无条件创建，
#     folder_mode=true 会变成「每条作品再单独套一层文件夹」。
# 这些都只在面板上**只读展示**，避免改出问题。
SETTING_FIELDS = {
    "max_pages": ("int", "每个号最多翻几页", "0 = 全部翻完；调小能大幅提速"),
    "max_size": ("int", "单文件体积上限 MB", "0 = 不限"),
    "timeout": ("int", "请求超时秒", ""),
    "max_retry": ("int", "失败重试次数", ""),
    "music": ("bool", "下载背景音乐", ""),
    "download": ("bool", "直接下载文件", "关掉就只打印不下载"),
}

# 只读展示（面板上灰字显示，不提供编辑）
READONLY_FIELDS = {
    "root": "下载根目录",
    "name_format": "文件命名格式",
    "folder_mode": "每条作品单独建文件夹",
    "folder_name": "单条作品模式的目录名",
}

# pyc 里的两个等待参数
PATCH_FIELDS = ("delay", "every", "rest")
OFF_EVERY = 1_000_000_000          # 「关掉批量暂停」用超大批次，避免 count % 0 崩


def _die(msg):
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
    sys.exit(0)


def running():
    try:
        # tasklist 在中文 Windows 上输出 GBK（0xd0 之类），不能按 utf-8 解，否则线程里抛异常
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + EXE_NAME],
                           capture_output=True, timeout=8)
        out = (p.stdout or b"").decode("mbcs", "ignore")
        return EXE_NAME.lower() in out.lower()
    except Exception:
        return False


# ---------------- settings.json ----------------
def load_settings():
    with open(SETTINGS, encoding="utf-8-sig") as f:
        return json.load(f)


def save_settings(data):
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8-sig", newline="\r\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, SETTINGS)


# ---------------- function.pyc ----------------
def load_pyc():
    with open(FUNC_PYC, "rb") as f:
        head = f.read(16)
        return head, marshal.load(f)


def dump_pyc(head, code):
    tmp = FUNC_PYC + ".new"
    with open(tmp, "wb") as f:
        f.write(head)
        marshal.dump(code, f)
    os.replace(tmp, FUNC_PYC)


def walk(co):
    yield co
    for c in co.co_consts:
        if isinstance(c, types.CodeType):
            yield from walk(c)


def read_pyc_values(top):
    sus = next((c for c in walk(top) if c.co_name == "suspend"), None)
    ints = [c for c in (sus.co_consts if sus else ()) if isinstance(c, int) and not isinstance(c, bool)]
    tup = [c for c in top.co_consts
           if isinstance(c, tuple) and len(c) == 2 and all(isinstance(x, float) for x in c)]
    return {
        "every": ints[0] if len(ints) > 0 else None,
        "rest": ints[1] if len(ints) > 1 else None,
        "delay": tup[0][0] if tup else None,
        "sigma": tup[0][1] if tup else None,
    }


def patch_pyc(delay=None, every=None, rest=None):
    head, top = load_pyc()

    def fix(co):
        consts = [fix(c) if isinstance(c, types.CodeType) else c for c in co.co_consts]
        if co.co_name == "suspend":
            idx = [i for i, c in enumerate(consts) if isinstance(c, int) and not isinstance(c, bool)]
            if every is not None and len(idx) > 0:
                consts[idx[0]] = int(every)
            if rest is not None and len(idx) > 1:
                consts[idx[1]] = int(rest)
        if co.co_name == "<module>" and delay is not None:
            consts = [((float(delay), c[1]) if isinstance(c, tuple) and len(c) == 2
                       and all(isinstance(x, float) for x in c) else c) for c in consts]
        return co.replace(co_consts=tuple(consts))

    new = fix(top)
    if not os.path.exists(PYC_BACKUP):
        os.makedirs(paths.BACKUP_DIR, exist_ok=True)
        shutil.copy2(FUNC_PYC, PYC_BACKUP)
    dump_pyc(head, new)
    # 立刻验一遍，改坏了就别等下次启动才发现
    _, back = load_pyc()
    return read_pyc_values(back)


# ---------------- 动作 ----------------
def do_read():
    if not os.path.isfile(SETTINGS):
        _die("找不到下载器配置：%s" % SETTINGS)
    s = load_settings()
    pyc = {}
    if os.path.isfile(FUNC_PYC):
        try:
            _, top = load_pyc()
            pyc = read_pyc_values(top)
        except Exception as e:
            pyc = {"error": "读取 pyc 失败：%s" % e}
    return {
        "ok": True,
        "running": running(),
        "settings": {k: s.get(k) for k in SETTING_FIELDS},
        "fields": {k: {"type": v[0], "label": v[1], "hint": v[2]} for k, v in SETTING_FIELDS.items()},
        "readonly": {k: s.get(k) for k in READONLY_FIELDS},
        "readonly_labels": READONLY_FIELDS,
        "pyc": pyc,
        "accounts": len(s.get("accounts_urls") or []),
        "paths": {"settings": SETTINGS, "pyc": FUNC_PYC, "pyc_backup": PYC_BACKUP},
    }


def do_write(payload):
    s = load_settings()
    incoming = payload.get("settings") or {}
    changed = {}
    for k, (typ, label, _hint) in SETTING_FIELDS.items():
        if k not in incoming:
            continue
        v = incoming[k]
        try:
            if typ == "int":
                v = int(float(v)) if v not in (None, "") else s.get(k, 0)
            elif typ == "bool":
                v = bool(v)
            else:
                v = str(v)
        except (TypeError, ValueError):
            _die("参数 %s（%s）的值不合法：%r" % (k, label, incoming[k]))
        if s.get(k) != v:
            s[k] = v
            changed[k] = v

    backup = ""
    if changed:
        os.makedirs(paths.BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = os.path.join(paths.BACKUP_DIR, "douk_settings_%s.json" % stamp)
        i = 2
        while os.path.exists(backup):      # 同一秒里连存两次，别互相覆盖
            backup = os.path.join(paths.BACKUP_DIR, "douk_settings_%s-%d.json" % (stamp, i))
            i += 1
        shutil.copy2(SETTINGS, backup)
        save_settings(s)

    pyc_new = {}
    pyc_want = payload.get("pyc") or {}
    delay = pyc_want.get("delay")
    every = pyc_want.get("every")
    rest = pyc_want.get("rest")
    if delay is not None or every is not None or rest is not None:
        cur = read_pyc_values(load_pyc()[1])
        need = ((delay is not None and abs(float(delay) - (cur.get("delay") or 0)) > 1e-6)
                or (every is not None and int(every) != cur.get("every"))
                or (rest is not None and int(rest) != cur.get("rest")))
        if need:
            pyc_new = patch_pyc(delay=delay, every=every, rest=rest)
        else:
            pyc_new = cur

    _, top = load_pyc()
    return {
        "ok": True,
        "changed": changed,
        "settings_backup": backup,
        "pyc": pyc_new or read_pyc_values(top),
        "pyc_backup": PYC_BACKUP,
        "running": running(),
        "warn": ("DouK 正在运行：这些参数要**重启 DouK** 才生效，而且它退出时可能用内存里的配置"
                 "覆盖 settings.json，建议先关掉再改。") if running() else "",
    }


def main():
    action = (sys.argv[1] if len(sys.argv) > 1 else "read").strip().lower()
    if sys.version_info[:2] != (3, 12):
        print(json.dumps({"ok": False, "error": "这个脚本要用 Python 3.12 跑（pyc 是 3.12 编的）"},
                         ensure_ascii=False))
        sys.exit(0)
    if action == "read":
        print(json.dumps(do_read(), ensure_ascii=False))
    elif action == "write":
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw or "{}")
        except ValueError as e:
            _die("请求体不是合法 JSON：%s" % e)
        print(json.dumps(do_write(payload), ensure_ascii=False))
    else:
        _die("未知动作：%s（只支持 read / write）" % action)


if __name__ == "__main__":
    main()
