#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「收藏 N 条以上」的博主导出成完整资料表 + DouK 批量链接。

流程：
  1. 读 export/收藏博主汇总_*.csv（含收藏数、样例视频）
  2. 按 sec_uid 批量补全抖音号 / 简介 / 认证 / 头像（DouK 签名 + im/user/info 接口，带本地缓存）
  3. 导出 xlsx（全部字段，格式化 + 说明页）
  4. 导出 txt（纯主页链接，一行一个，直接粘给 DouK-Downloader）

用法：
  python export_key_authors.py        # 默认收藏 5 条以上
  python export_key_authors.py 2      # 收藏 2 条以上
  python export_key_authors.py 5 --no-fetch   # 不补资料，只用现有字段
"""

import csv
import glob
import json
import os
import sys
import time
from datetime import datetime
from urllib.parse import urlencode

DOUK_DIR = r"E:\dyyyyyyyyyyyyydownload\TikTokDownloader_V5.6_Windows_X64\DouK-Downloader_V5.8_Windows_X64"
LIB = os.path.join(DOUK_DIR, "lib")
SETTINGS = os.path.join(DOUK_DIR, "Volume", "settings.json")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
import os as _os
try:
    _os.add_dll_directory(LIB)
    _p = os.path.join(LIB, "curl_cffi.libs")
    if _os.path.isdir(_p):
        _os.add_dll_directory(_p)
except Exception:
    pass

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_TOOLS)
import sys  # noqa: E402
sys.path.insert(0, _TOOLS)                       # tools/ 下的 blacklist、downloaded
sys.path.insert(0, os.path.join(_ROOT, "src"))   # src/paths.py 统一路径
import paths  # noqa: E402

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))   # 仅用于拼本目录内的东西
OUT_DIR = paths.EXPORT_DIR
CACHE = os.path.join(OUT_DIR, ".博主资料缓存.json")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
INFO_URL = "https://www.douyin.com/aweme/v1/web/im/user/info/"


# ---------------- 资料补全 ----------------
def fetch_profiles(sec_uids):
    """批量查 sec_uid → {sec_uid: {unique_id, short_id, signature, verify, avatar, nickname}}"""
    cache = {}
    if os.path.isfile(CACHE):
        try:
            cache = json.load(open(CACHE, encoding="utf-8"))
        except Exception:
            cache = {}
    todo = [s for s in sec_uids if s and s not in cache]
    print(f"[资料] 缓存已有 {len(cache)} 个，需查询 {len(todo)} 个")
    if not todo:
        return cache

    os.chdir(DOUK_DIR)
    from curl_cffi import requests as cr
    from src.encrypt.aBogus import ABogus

    st = json.load(open(SETTINGS, encoding="utf-8-sig"))
    ck = st.get("cookie") or {}
    bi = st.get("browser_info") or {}
    ab = ABogus(user_agent=UA)
    base_q = {
        "device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
        "pc_client_type": "1", "version_code": "170400", "version_name": "17.4.0",
        "cookie_enabled": "true", "screen_width": "1536", "screen_height": "864",
        "browser_language": bi.get("browser_language", "zh-CN"),
        "browser_platform": bi.get("browser_platform", "MacIntel"),
        "browser_name": bi.get("browser_name", "Chrome"),
        "browser_version": bi.get("browser_version", "146.0.0.0"),
        "engine_name": "Blink", "engine_version": "146.0.0.0",
        "os_name": bi.get("os_name", "Mac OS"), "os_version": bi.get("os_version", "10.15.7"),
        "platform": "PC",
    }
    headers = {
        "User-Agent": UA, "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.douyin.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "x-tt-argus": "1", "uifid": str(ck.get("UIFID") or ""),
        "Cookie": "; ".join(f"{k}={v}" for k, v in ck.items() if v not in (None, "")),
    }

    BATCH = 20
    ok = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        body = urlencode({"sec_user_ids": json.dumps(chunk)})
        got = None
        for attempt in range(3):
            try:
                q = dict(base_q)
                q["a_bogus"] = ab.get_value(urlencode(q), body=body,
                                            content_type="application/x-www-form-urlencoded")
                r = cr.post(INFO_URL, params=q, data=body, headers=headers,
                            impersonate="chrome146", timeout=30)
                if r.status_code == 200:
                    got = r.json()
                    break
                time.sleep(1.5)
            except Exception:
                time.sleep(1.5)
        if not got:
            print(f"  [!] 第 {i // BATCH + 1} 批失败，跳过")
            continue
        for u in (got.get("data") or []):
            sid = u.get("sec_uid")
            if not sid:
                continue
            av = u.get("avatar_small") or {}
            urls = av.get("url_list") or []
            cache[sid] = {
                "unique_id": u.get("unique_id") or "",
                "short_id": u.get("short_id") or "",
                "signature": (u.get("signature") or "").replace("\n", " / "),
                "verify": u.get("custom_verify") or u.get("enterprise_verify_reason") or "",
                "avatar": urls[0] if urls else "",
                "nickname": u.get("nickname") or "",
            }
            ok += 1
        print(f"  进度 {min(i + BATCH, len(todo))}/{len(todo)} · 已补 {ok}")
        time.sleep(0.4)

    json.dump(cache, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"[资料] 本次补到 {ok} 个，缓存累计 {len(cache)} 个")
    return cache


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    min_n = int(args[0]) if args else 5
    no_fetch = "--no-fetch" in sys.argv

    src = max(glob.glob(os.path.join(OUT_DIR, "收藏博主汇总_*.csv")), key=os.path.getmtime)
    rows = list(csv.DictReader(open(src, encoding="utf-8-sig")))
    rows = [r for r in rows if int(r["count"]) >= min_n]

    # 黑名单剔除
    sys.path.insert(0, SCRIPT_DIR)
    import blacklist as BL
    kept = []
    blocked = []
    for r in rows:
        if BL.get().match(sec_uid=r.get("sec_uid", ""), uid=r.get("uid", ""), nickname=r.get("nickname", "")):
            blocked.append(r)
        else:
            kept.append(r)
    if blocked:
        print(f"[黑名单] 剔除 {len(blocked)} 个不下载的博主：" +
              "、".join((b.get("nickname") or "?") for b in blocked))
    rows = kept
    rows.sort(key=lambda r: -int(r["count"]))
    print(f"[输入] {os.path.basename(src)} → 收藏≥{min_n}条 共 {len(rows)} 个博主")

    profiles = {} if no_fetch else fetch_profiles([r["sec_uid"] for r in rows])

    for r in rows:
        p = profiles.get(r["sec_uid"]) or {}
        r["douyin_id"] = r.get("douyin_id") or p.get("unique_id") or p.get("short_id") or ""
        r["signature"] = p.get("signature", "")
        r["verify"] = p.get("verify", "")
        r["avatar"] = p.get("avatar", "")
        aid = r.get("sample_aweme") or ""
        r["sample_url"] = f"https://www.douyin.com/video/{aid}" if aid else ""

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    tag = f"收藏{min_n}条以上"

    # ---------------- xlsx ----------------
    COLS = [
        ("序号", "idx", 6),
        ("昵称", "nickname", 22),
        ("收藏数", "count", 8),
        ("抖音号", "douyin_id", 16),
        ("UID", "uid", 19),
        ("认证", "verify", 14),
        ("简介", "signature", 34),
        ("最新作品时间", "last_work", 16),
        ("下载状态", "downloaded", 9),
        ("已下载备注名", "known_as", 14),
        ("sec_uid", "sec_uid", 60),
        ("主页链接", "author_url", 66),
        ("样例收藏视频", "sample_url", 44),
        ("头像", "avatar", 40),
    ]

    wb = Workbook()
    ws = wb.active
    ws.title = f"{tag}博主"

    head_fill = PatternFill("solid", fgColor="1F3864")
    head_font = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
    body_font = Font(name="微软雅黑", size=10)
    link_font = Font(name="微软雅黑", size=10, color="0563C1", underline="single")
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    f_undone = PatternFill("solid", fgColor="FFF2CC")
    f_done = PatternFill("solid", fgColor="E2EFDA")
    center = Alignment(horizontal="center", vertical="center", wrap_text=False)
    left = Alignment(horizontal="left", vertical="center", wrap_text=False)

    for i, (label, _k, w) in enumerate(COLS, 1):
        c = ws.cell(row=1, column=i, value=label)
        c.fill, c.font, c.alignment, c.border = head_fill, head_font, center, border
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 26

    for ridx, r in enumerate(rows, 2):
        done = r["downloaded"] == "已下载"
        for cidx, (_l, key, _w) in enumerate(COLS, 1):
            if key == "idx":
                val = ridx - 1
            elif key == "count":
                val = int(r["count"])
            else:
                val = r.get(key, "") or ""
            c = ws.cell(row=ridx, column=cidx, value=val)
            c.font, c.border = body_font, border
            c.alignment = left if key in ("nickname", "signature", "sec_uid", "verify") else center
            c.fill = f_done if done else f_undone
            if key in ("author_url", "sample_url", "avatar") and val:
                c.hyperlink = val
                c.font = link_font
        ws.row_dimensions[ridx].height = 19

    ws.freeze_panes = "B2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLS))}{len(rows) + 1}"

    # 说明页
    ws2 = wb.create_sheet("说明")
    n_done = sum(1 for r in rows if r["downloaded"] == "已下载")
    notes = [
        ["数据来源", "抖音「我的收藏」接口 listcollection；博主资料来自 im/user/info（均按 sec_uid 权威匹配）"],
        ["导出时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ["筛选口径", f"同一博主被收藏的作品数 ≥ {min_n} 条，按收藏数降序"],
        ["博主总数", len(rows)],
        ["已下载过", n_done],
        ["未下载", len(rows) - n_done],
        ["状态判定", "与本地 data.json / user_data.json / uid_secuid_对照表.csv 按 uid + sec_uid 双键比对"],
        ["底色说明", "浅黄 = 未下载（值得补）；浅绿 = 已下载"],
        ["配套 txt", f"重点博主_未下载_DouK批量_{min_n}条_*.txt（纯链接，一行一个，直接粘给 DouK）"],
        ["秒传提示", "抖音号为空说明该账号未设置自定义抖音号，只有系统 short_id"],
    ]
    for i, (k, v) in enumerate(notes, 1):
        ws2.cell(row=i, column=1, value=k).font = Font(name="微软雅黑", size=10, bold=True)
        c = ws2.cell(row=i, column=2, value=v)
        c.font = body_font
        c.alignment = left
    ws2.column_dimensions["A"].width = 14
    ws2.column_dimensions["B"].width = 100

    f_xlsx = os.path.join(OUT_DIR, f"收藏{min_n}条以上博主_完整信息_{stamp}.xlsx")
    wb.save(f_xlsx)

    # ---------------- txt ----------------
    pend = [r for r in rows if r["downloaded"] == "未下载" and r.get("author_url")]
    f_txt_new = os.path.join(OUT_DIR, f"重点博主_未下载_DouK批量_{min_n}条_{stamp}.txt")
    with open(f_txt_new, "w", encoding="utf-8", newline="\n") as f:
        for r in pend:
            f.write(r["author_url"] + "\n")

    f_txt_all = os.path.join(OUT_DIR, f"重点博主_全部_DouK批量_{min_n}条_{stamp}.txt")
    with open(f_txt_all, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            if r.get("author_url"):
                f.write(r["author_url"] + "\n")

    filled = sum(1 for r in rows if r["douyin_id"])
    print("\n" + "=" * 58)
    print(f"博主 {len(rows)} 个 · 已下载 {n_done} / 未下载 {len(pend)} · 补到抖音号 {filled} 个")
    print("=" * 58)
    print(f"[xlsx]      {f_xlsx}")
    print(f"[txt·未下载] {f_txt_new}  （{len(pend)} 行，推荐）")
    print(f"[txt·全部]   {f_txt_all}  （{len(rows)} 行）")


if __name__ == "__main__":
    main()
