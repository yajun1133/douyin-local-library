#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「未下载博主主页链接」写进 DouK 的 settings.json → accounts_urls 参数，
避免终端粘贴 225 行 txt 卡住。

流程（严格顺序）：
  1. 备份 settings.json 到 docs/（用户指定）+ browser/backup/（双保险）
  2. 校验备份可读、字节数与原文一致
  3. 只在 accounts_urls 上做替换，其它字段逐字节保留（重新序列化后比对）
  4. 写回：保持 UTF-8 with BOM + CRLF + 4 空格缩进
  5. 读回校验

用法：
  python apply_accounts_urls.py            # 用最新 txt，只放未下载的
  python apply_accounts_urls.py --all      # 放全部 500 个
  python apply_accounts_urls.py --dry      # 只看会写什么，不落盘
"""

import glob
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime

DOUK_ROOT = r"E:\dyyyyyyyyyyyyydownload\TikTokDownloader_V5.6_Windows_X64"
DOUK_DIR = os.path.join(DOUK_ROOT, "DouK-Downloader_V5.8_Windows_X64")
SETTINGS = os.path.join(DOUK_DIR, "Volume", "settings.json")
DOC_BACKUP_DIR = os.path.join(DOUK_ROOT, "文档")

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
import sys  # noqa: E402
sys.path.insert(0, _TOOLS)                       # tools/ 下的 blacklist、downloaded
sys.path.insert(0, os.path.join(_ROOT, "src"))   # src/paths.py 统一路径
import paths  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # 仅用于拼本目录内的东西
OUT_DIR = paths.EXPORT_DIR
LOCAL_BACKUP_DIR = paths.BACKUP_DIR


def read_settings():
    raw = open(SETTINGS, "rb").read()
    return raw, json.loads(raw.decode("utf-8-sig"))


def write_settings(data):
    txt = json.dumps(data, ensure_ascii=False, indent=4)
    txt = txt.replace("\r\n", "\n").replace("\n", "\r\n")
    raw = b"\xef\xbb\xbf" + txt.encode("utf-8")
    tmp = SETTINGS + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, SETTINGS)
    return raw


def fingerprint(obj):
    """除 accounts_urls 外所有字段的指纹，用来证明没动别的配置。"""
    clone = {k: v for k, v in obj.items() if k != "accounts_urls"}
    s = json.dumps(clone, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def main():
    dry = "--dry" in sys.argv
    use_all = "--all" in sys.argv

    pattern = os.path.join(OUT_DIR, "重点博主_全部_DouK批量_5条_*.txt" if use_all
                           else "重点博主_未下载_DouK批量_5条_*.txt")
    files = glob.glob(pattern)
    if not files:
        sys.exit(f"[!] 找不到链接清单：{pattern}")
    txt_path = max(files, key=os.path.getmtime)
    urls = [l.strip() for l in open(txt_path, encoding="utf-8") if l.strip()]
    print(f"[链接] {os.path.basename(txt_path)} → {len(urls)} 个博主")

    # ---- 昵称映射 + 黑名单剔除 ----
    import csv as _csv
    import re as _re
    sys.path.insert(0, SCRIPT_DIR)
    import blacklist as BL

    nick = {}
    uid_of = {}
    for f in glob.glob(os.path.join(OUT_DIR, "收藏博主汇总_*.csv")):
        for row in _csv.DictReader(open(f, encoding="utf-8-sig")):
            if row.get("author_url"):
                nick[row["author_url"]] = row.get("nickname") or ""
                if row.get("uid"):
                    uid_of[row["author_url"]] = str(row["uid"])
        break

    def _sec(u):
        m = _re.search(r"MS4wLjAB[A-Za-z0-9_\-]+", u)
        return m.group(0) if m else ""

    blocked, kept = [], []
    for u in urls:
        hit = BL.get().match(sec_uid=_sec(u), nickname=nick.get(u, ""))
        if hit:
            blocked.append((nick.get(u, ""), u))
        else:
            kept.append(u)
    if blocked:
        print(f"[黑名单] 剔除 {len(blocked)} 个不需要下载的博主：")
        for n, u in blocked:
            print(f"    - {n or '(无昵称)'}  {u[-12:]}")
        urls = kept
    else:
        print("[黑名单] 无需剔除")

    # ---- 已下载剔除（以下载目录为准，避免重复下）----
    if "--redo" in sys.argv:
        print("[已下载] --redo：保留已下载的博主")
    else:
        try:
            import downloaded as DL
            dl = DL.get()
            redone = [u for u in urls
                      if dl.is_downloaded(uid=uid_of.get(u, ""), nickname=nick.get(u, ""))]
            if redone:
                print(f"[已下载] 剔除 {len(redone)} 个已经下过的博主"
                      f"（想重下加 --redo）")
                urls = [u for u in urls if u not in set(redone)]
            else:
                print("[已下载] 队列里没有已下载的博主")
        except Exception as e:
            print(f"[提示] 已下载过滤跳过：{e}")

    raw, cfg = read_settings()
    before_fp = fingerprint(cfg)
    old = cfg.get("accounts_urls") or []
    print(f"[现状] settings.json {len(raw)} 字节 · accounts_urls 现有 {len(old)} 条")

    # ---------- 1. 备份 ----------
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak_name = f"settings_备份_{stamp}.json"
    os.makedirs(DOC_BACKUP_DIR, exist_ok=True)
    os.makedirs(LOCAL_BACKUP_DIR, exist_ok=True)
    doc_bak = os.path.join(DOC_BACKUP_DIR, bak_name)
    local_bak = os.path.join(LOCAL_BACKUP_DIR, bak_name)
    if not dry:
        shutil.copy2(SETTINGS, doc_bak)
        shutil.copy2(SETTINGS, local_bak)
        # ---------- 2. 校验备份 ----------
        for p in (doc_bak, local_bak):
            b = open(p, "rb").read()
            assert len(b) == len(raw), f"备份大小不一致: {p}"
            json.loads(b.decode("utf-8-sig"))          # 能解析
        print(f"[备份✓] {doc_bak}")
        print(f"[备份✓] {local_bak}")

    # ---------- 3. 构造 accounts_urls ----------
    new_items = []
    for u in urls:
        new_items.append({
            "mark": nick.get(u, "")[:20],
            "url": u,
            "tab": "",
            "earliest": "",
            "latest": "",
            "enable": True,
        })
    cfg["accounts_urls"] = new_items

    if dry:
        print("\n[dry] 将写入的前 3 条：")
        for x in new_items[:3]:
            print("   ", json.dumps(x, ensure_ascii=False))
        print(f"[dry] 未落盘。备份文件名为 {bak_name}")
        return

    # ---------- 4. 写回 ----------
    new_raw = write_settings(cfg)
    print(f"[写入] settings.json {len(new_raw)} 字节（原 {len(raw)}）")

    # ---------- 5. 校验 ----------
    raw2, cfg2 = read_settings()
    assert fingerprint(cfg2) == before_fp, "除 accounts_urls 外的配置被改动了！"
    assert len(cfg2["accounts_urls"]) == len(urls)
    assert all(x["url"].startswith("https://www.douyin.com/user/") for x in cfg2["accounts_urls"])
    print(f"[校验✓] 其它配置未变（指纹 {before_fp}）")
    print(f"[校验✓] accounts_urls 共 {len(cfg2['accounts_urls'])} 条，全部为合法主页链接")
    print(f"[回滚] 出问题就用备份覆盖回去：{doc_bak}")


if __name__ == "__main__":
    main()
