#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音下载目录扫描器
遍历 downnnnnnnn 目录，生成 data.json 索引文件，供本地网页浏览。

用法:
    python scanner.py            # 增量扫描（只重扫 mtime 变了的作者文件夹；无变化不写盘）
    python scanner.py --full     # 强制全量重扫

增量原理：根目录 `scandir` 一次就能拿到每个作者文件夹的 mtime（Windows 上 DirEntry.stat()
读的是目录枚举缓存，不额外读盘）。文件夹内容一变（新增/删除/改名文件）目录 mtime 必变，
所以「谁变了」一次列目录就知道，只有变了的才真去读文件。mtime 记在
`data/scan_mtimes.json`，重扫时还会把 `sec_uid/douyin_id/signature/cover` 这些
「不是从文件名推出来的」字段从旧 data.json 里搬回来，不会因为重扫丢数据。

输出 data.json 结构:
{
  "meta": {"scan_time": "...", "total_authors": N},
  "accounts": {
     "100301438759": {
        "uid": "...", "nickname": "...", "sec_uid": "...",
        "folder": "UID..._昵称_发布作品",
        "works": [ {"date": "...", "desc": "...", "media_type": "...", "files": [...] } ],
        "stats": {...}
     }
  }
}

说明: 只扫描作者「发布作品」文件夹。直播、收藏夹、话题标签均不在本扫描范围内。
"""

import os
import re
import json
import sys
import time
import html as _html
from collections import defaultdict

# ============ 路径配置（统一来自 paths.py） ============
import paths
import download_db    # 下载记录库（谁什么时候下过、多少文件）

BASE_DIR = paths.MEDIA_BASE
SCAN_ROOT = paths.SCAN_ROOT
# 下载器的 xlsx 目录（注意不是 paths.DATA_DIR，那个是本项目的数据目录）
DATA_DIR = os.path.join(SCAN_ROOT, "Data")

OUTPUT_JSON = paths.DATA_JSON                # .../browser/data/data.json

# 支持的媒体扩展名
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv"}
IMAGE_EXTS = {".jpeg", ".jpg", ".png", ".webp", ".gif"}

# 文件夹命名正则（只匹配作者发布作品）
UID_FOLDER_RE = re.compile(r"^UID(\d+)_(.*)_发布作品$")

# 少部分老目录当初拿不到昵称，直接把 sec_uid 当名字用了（下载器抓不到主页资料时的兜底），
# 于是文件夹名长成 `UID123_MS4wLjABAAAA…_发布作品`。这种中间那段其实**是 sec_uid 不是昵称**：
# 必须还原（昵称留空、把 sec_uid 补上），否则列表里会顶着一长串 MS4wLjAB…（2026-11-05 用户报的 bug）
SEC_UID_LIKE = re.compile(r"^MS4wLjAB[\w-]{16,}$")


def looks_like_sec_uid(s):
    """这个字符串看着像不像一个 sec_uid（而不是人取的昵称）"""
    return bool(s and SEC_UID_LIKE.match(str(s).strip()))

# 文件名解析: 日期-昵称-描述_序号.扩展名
# 描述中可能包含 _数字 后缀（图文序号），用非贪婪匹配 + 末尾 _(\d+) 处理
FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.*?)-(.*?)(?:_(\d+))?\.([A-Za-z0-9]+)$")
# 两段式：`日期-昵称[_序号].ext` —— 这条作品**没有描述**。
# 老版 DouK 对空描述留一个尾横杠（`日期-昵称-.mp4`，主正则可以解析成 desc=""）；
# 2026-11-02 统一去掉文件名末尾的作品 ID 之后，空描述会变成更干净的 `日期-昵称.mp4`，
# 主正则要求两段 `-` 就匹配不上了，会被当成「无法解析」丢进 raw 作品（日期空、标题是文件名）。
# 历史上还有一批 `2022-11-01-SoilfIreiIl_1.jpeg` 这种同样只有两段的，一并救回来。
FILE_RE_TWO = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+?)(?:_(\d+))?\.([A-Za-z0-9]+)$")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def phase(msg):
    """给 server.py 用的阶段标记（它读 stdout 转成前端进度文字）"""
    print(f"PHASE {msg}", flush=True)


def progress(done, total):
    """给 server.py 用的进度标记（PROGRESS 已处理/总数）"""
    print(f"PROGRESS {done}/{total}", flush=True)


def _desc_rank(d):
    """描述「像人话」的程度：空 = 0，纯数字（DouK 拿 ID 当描述）= 1，有文字 = 2。

    同一个作品的两份文件可能一份有描述、一份只有 ID 兜底，聚合时取排名高的那个。
    """
    if not d:
        return 0
    return 1 if re.fullmatch(r"\d+", str(d)) else 2


def parse_work_filename(filename):
    """解析作品文件名，返回 (date, author, desc, seq, ext) 或 None"""
    m = FILE_RE.match(filename)
    if not m:
        # 两段式兜底：`日期-昵称[_n].ext`，描述为空（见 FILE_RE_TWO 的注释）
        m2 = FILE_RE_TWO.match(filename)
        if not m2:
            return None
        date, author, seq, ext = m2.groups()
        return {"date": date, "author": author, "desc": "", "seq": seq,
                "ext": ext.lower(), "id": ""}
    date, author, desc, seq, ext = m.groups()
    # 末尾那串 15~20 位数字就是 DouK 的作品 ID，剥掉别让它显示在描述里。
    # 两种来源都要处理：
    #   ① 2026-10-31 起 name_format 末尾加了 id → 「日期-昵称-描述-<id>.mp4」
    #   ② 更老的文件里「描述为空 → DouK 拿 ID 当描述」→ 「日期-昵称-<id>.mp4」
    # 所以判据是「desc 末尾是独立的数字段」，不是「desc 整个是数字」。
    aweme_id = ""
    mm = re.search(r"(?:^|-)(\d{15,20})$", desc)
    if mm:
        aweme_id = mm.group(1)
        desc = desc[:mm.start()].rstrip("-") if mm.start() > 0 else ""
    # 「日期-昵称-<id>-<id>.mp4」这种双 ID 名字（重复下载的产物）剥完末尾还剩一个 ID，
    # 它本来就是「这条作品没有描述」，别让人在界面上看到一串数字当标题。
    if aweme_id and desc == aweme_id:
        desc = ""
    return {"date": date, "author": author, "desc": desc, "seq": seq,
            "ext": ext.lower(), "id": aweme_id}


def scan_folder(folder_path):
    """扫描单个文件夹，返回媒体文件列表"""
    files = []
    try:
        entries = os.listdir(folder_path)
    except OSError:
        return files

    for name in sorted(entries):
        full = os.path.join(folder_path, name)
        if os.path.isdir(full):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in VIDEO_EXTS and ext not in IMAGE_EXTS:
            continue
        try:
            st = os.stat(full)
            size = st.st_size
            # 文件写入时间就是「这条作品什么时候下载下来的」——下载记录库靠它算
            # 「这个作者多久没下载过了」。同一个 stat 顺手带出来，不额外读盘。
            mtime = int(st.st_mtime)
        except OSError:
            size = 0
            mtime = 0
        rel = os.path.relpath(full, SCAN_ROOT).replace("\\", "/")
        files.append({
            "name": name,
            "rel": rel,
            "ext": ext,
            "size": size,
            "mtime": mtime,
            "parsed": parse_work_filename(name),
        })
    return files


def scan_author_folder(folder_name):
    """扫描作者文件夹，返回 account 数据 dict"""
    m = UID_FOLDER_RE.match(folder_name)
    if not m:
        return None
    uid = m.group(1)
    nickname = m.group(2)
    # 目录名里那一段可能是 sec_uid 而不是昵称（见 SEC_UID_LIKE 的注释）：认出来就还原，
    # 昵称留空等「获取资料」补，同时把 sec_uid 补上（这类账号原本 sec_uid 是空的）
    sec_uid_guess = ""
    if looks_like_sec_uid(nickname):
        sec_uid_guess = nickname.strip()
        nickname = ""

    folder_path = os.path.join(SCAN_ROOT, folder_name)
    files = scan_folder(folder_path)

    # 目录名没给出可用昵称（那一段是 sec_uid，或者干脆空着）→ 从作品文件名里捞一个。
    # 文件名是 `日期-昵称-描述.ext`，第二段就是下载当时的昵称，比目录名可信。
    if not nickname:
        for _f in files:
            _a = ((_f.get("parsed") or {}).get("author") or "").strip()
            if _a and not looks_like_sec_uid(_a):
                nickname = _a
                break

    # 按作品聚合（同一日期+描述的一组图片算一个作品）
    works_map = defaultdict(lambda: {"date": "", "desc": "", "media_type": "", "files": []})
    video_count = 0
    image_count = 0

    for f in files:
        p = f["parsed"]
        ext = f["ext"]
        if ext in VIDEO_EXTS:
            video_count += 1
            media = "video"
        else:
            image_count += 1
            media = "image"

        if p:
            # 2026-11-02：**有作品 ID 就按 ID 聚合**。同一个 aweme_id 一定是同一个作品，
            # 哪怕文件名因为「改了命名模板 / 作者改名 / 描述被清空」而变得不一样
            # —— 按「日期+描述」聚合时这类会裂成两个作品（一个标题正常、一个是一串数字）。
            # 没有 ID 的老文件（更早期别的工具下的）退回原来的「日期+描述」，否则会把不相关的合成一个。
            key = ("id", p["id"]) if p["id"] else ("dd", p["date"], p["desc"])
            w = works_map[key]
            w["date"] = p["date"]
            # 描述取「更像人话的那条」：空 / 纯数字是 DouK 的 ID 兜底值，
            # 不能让它盖掉同作品另一份文件上的真描述（比如 #黑爵）。
            if _desc_rank(p["desc"]) > _desc_rank(w["desc"]):
                w["desc"] = p["desc"]
            if media == "video":
                w["media_type"] = "video"
            elif not w["media_type"]:
                w["media_type"] = "image"
            seq = int(p["seq"]) if p["seq"] is not None else 0
            w["files"].append((seq, f["rel"], f["name"]))
        else:
            # 无法解析的文件名，单独作为一条
            key = ("__raw__", f["name"])
            w = works_map[key]
            w["date"] = ""
            w["desc"] = f["name"]
            if media == "video":
                w["media_type"] = "video"
            elif not w["media_type"]:
                w["media_type"] = "image"
            w["files"].append((0, f["rel"], f["name"]))

    works = []
    for key, w in works_map.items():
        # 视频排前面：同一个作品可能同时有「封面 _1.jpeg」+「视频 _2.mp4」，
        # 之前按序号排会让 jpeg 抢到 files[0]，前端就当视频放，必然放不出来。
        w["files"].sort(key=lambda x: (0 if os.path.splitext(x[1])[1].lower() in VIDEO_EXTS else 1, x[0]))
        # 只保留 rel（路径），不存冗余的 name（可从 rel 提取扩展名）
        w["files"] = [{"rel": rel} for _, rel, name in w["files"]]
        # 细分作品类型：video / image_single / image_multi
        n_files = len(w["files"])
        if w["media_type"] == "video":
            w["media_type"] = "video"
        else:
            w["media_type"] = "image_single" if n_files <= 1 else "image_multi"
        w["file_count"] = n_files
        works.append(w)

    # 按日期排序（新的在前）
    works.sort(key=lambda w: w["date"], reverse=True)

    dates = [w["date"] for w in works if w["date"]]
    earliest = min(dates) if dates else ""
    latest = max(dates) if dates else ""

    return {
        "uid": uid,
        "nickname": nickname,
        "sec_uid": sec_uid_guess,
        "douyin_id": "",
        "signature": "",
        "folder": folder_name,
        "folder_rel": folder_name,
        "folders": [folder_name],
        "works": works,
        # 原始文件清单（带 size/mtime），只给下载记录库用，写 data.json 前必须 pop 掉
        "_raw_files": files,
        "stats": {
            "video_count": video_count,
            "image_count": image_count,
            "work_count": len(works),
            "earliest": earliest,
            "latest": latest,
        },
    }


XLSX_CACHE = os.path.join(paths.DATA_DIR, "uid_sec_cache.json")
# 抖音侧「最新发布时间 / 最后采集时间」，来自下载器的作品表 xlsx（见 _parse_xlsx_meta）
REMOTE_META = os.path.join(paths.DATA_DIR, "remote_latest.json")


def _remote_meta_save(uid_meta):
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        tmp = REMOTE_META + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"version": 1, "authors": uid_meta}, fp, ensure_ascii=False)
        os.replace(tmp, REMOTE_META)
    except OSError as e:
        log("发布时间缓存写入失败（不影响索引）: %s" % e)


def _sec_cache_load():
    try:
        with open(XLSX_CACHE, "r", encoding="utf-8") as fp:
            d = json.load(fp)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _sec_cache_save(files):
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        tmp = XLSX_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"version": 1, "files": files}, fp, ensure_ascii=False)
        os.replace(tmp, XLSX_CACHE)
    except OSError as e:
        log("sec_uid 缓存写入失败（不影响结果）: %s" % e)


def _parse_xlsx_meta(path):
    """读一个 xlsx：表头 + 数据行，取 (uid, sec_uid) 和两个日期。

    返回 {"pair": [uid, sec_uid] 或 None,
          "pub":   "发布时间"列的最大值（= 抖音上这个作者最新一条作品的发布时间）,
          "crawl": "采集时间"列的最大值（= 下载器最后一次抓到他的时间）}

    为什么要 pub：详情页「抖音 N」后面要显示「发布 2025-08-30」，而抖音的公开接口
    只给作品数、不给发布时间（iesdouyin 的 aweme/post 返回空 / 403，要 a_bogus 签名）。
    下载器每跑完一个作者都会把作品表写成 xlsx，里面正好有「发布时间」「采集时间」两列，
    所以直接从这里取——不联网，也不额外读盘（这张表本来就要打开找 sec_uid）。

    实现上**不用 ElementTree**：一张表动辄 900+ 行 × 30 列，ET 建完整棵树要 140ms，
    660 个作者表就是 90 秒。这里只按正则抠出目标那几列（B/DA/M 之类），单表 ~20ms。
    """
    import zipfile

    out = {"pair": None, "pub": "", "crawl": ""}
    try:
        # 必须用 with：异常时不 close，Windows 下 zip 句柄会一直占着 Data/ 里的 xlsx
        with zipfile.ZipFile(path) as z:
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "replace")
    except Exception:
        return out

    row_re = re.compile(r"<row\b[^>]*>(.*?)</row>", re.S)
    cell_re = re.compile(r"<c\b([^>]*)>(.*?)</c>", re.S)
    col_re = re.compile(r'r="([A-Z]+)\d+"')

    def cell_text(attrs, inner):
        m = col_re.search(attrs)
        col = m.group(1) if m else ""
        if 't="inlineStr"' in attrs:
            # xlsx 里的中文是 &#20316;&#21697; 这种实体，必须还原，否则表头永远匹配不上
            parts = re.findall(r"<t[^>]*>(.*?)</t>", inner, re.S)
            val = _html.unescape("".join(parts))
        else:
            vm = re.search(r"<v>(.*?)</v>", inner, re.S)
            val = vm.group(1) if vm else ""
        return col, val

    rows = row_re.findall(sheet)
    if len(rows) < 2:
        return out

    header_map = {}
    for attrs, inner in cell_re.findall(rows[0]):
        col, val = cell_text(attrs, inner)
        if col:
            header_map[val.strip()] = col
    uid_col = header_map.get("UID")
    sec_col = header_map.get("SEC_UID")
    pub_col = header_map.get("发布时间")
    crawl_col = header_map.get("采集时间")
    want = {c for c in (uid_col, sec_col, pub_col, crawl_col) if c}
    if not want:
        return out

    pub = crawl = ""
    first = True
    for row in rows[1:]:
        cells = {}
        for attrs, inner in cell_re.findall(row):
            m = col_re.search(attrs)
            col = m.group(1) if m else ""
            if col in want:
                _, cells[col] = cell_text(attrs, inner)
        if first:
            first = False
            uid_val = (cells.get(uid_col) or "").strip()
            sec_val = (cells.get(sec_col) or "").strip()
            if uid_val and sec_val:
                out["pair"] = [uid_val, sec_val]
        if pub_col:
            d = (cells.get(pub_col) or "").strip()[:10]
            if len(d) >= 10 and d > pub:
                pub = d
        if crawl_col:
            d = (cells.get(crawl_col) or "").strip()[:10]
            if len(d) >= 10 and d > crawl:
                crawl = d
    out["pub"] = pub
    out["crawl"] = crawl
    return out


def extract_sec_uid_from_xlsx():
    """从 Data 目录的 xlsx 中提取 uid -> sec_uid 映射，外加每个作者的「发布时间 / 采集时间」。

    返回 (uid_to_sec, uid_meta)，uid_meta[uid] = {"pub": "YYYY-MM-DD", "crawl": "YYYY-MM-DD"}

    原来每次扫描都要把上千个 xlsx 逐个解压读 sheet1，实测要 **31.7 秒** ——
    增量扫描本来 1 秒就跑完了，全被这一步拖住。xlsx 只在下载器写新作品时才变，
    所以按 name+mtime+size 记住解析结果，没变的文件不再解压。
    """
    uid_to_sec = {}
    uid_meta = {}
    if not os.path.isdir(DATA_DIR):
        return uid_to_sec, uid_meta

    cache = _sec_cache_load()
    files_cache = cache.get("files") if isinstance(cache.get("files"), dict) else {}
    changed = False
    seen = set()
    try:
        xlsx_files = sorted(f for f in os.listdir(DATA_DIR) if f.lower().endswith(".xlsx"))
    except OSError:
        return uid_to_sec, uid_meta

    for xf in xlsx_files:
        seen.add(xf)
        path = os.path.join(DATA_DIR, xf)
        try:
            st = os.stat(path)
        except OSError:
            continue
        ent = files_cache.get(xf)
        if (isinstance(ent, dict) and ent.get("mt") == st.st_mtime
                and ent.get("size") == st.st_size and "pair" in ent and "pub" in ent):
            pair = ent.get("pair")
            pub = ent.get("pub") or ""
            crawl = ent.get("crawl") or ""
        else:
            meta = _parse_xlsx_meta(path)
            pair = meta.get("pair")
            pub = meta.get("pub") or ""
            crawl = meta.get("crawl") or ""
            files_cache[xf] = {"mt": st.st_mtime, "size": st.st_size,
                               "pair": pair, "pub": pub, "crawl": crawl}
            changed = True
        if pair and pair[0] and pair[1] and pair[0] not in uid_to_sec:
            uid_to_sec[pair[0]] = pair[1]
        if pair and pair[0] and (pub or crawl):
            prev = uid_meta.get(pair[0]) or {}
            uid_meta[pair[0]] = {
                "pub": max(prev.get("pub") or "", pub),
                "crawl": max(prev.get("crawl") or "", crawl),
            }

    for k in list(files_cache):
        if k not in seen:
            files_cache.pop(k, None)
            changed = True
    if changed:
        _sec_cache_save(files_cache)
    return uid_to_sec, uid_meta


def merge_accounts(existing, new):
    """合并同一 UID 的多个文件夹（昵称变更导致），记录昵称历史"""
    # 昵称历史：如果新昵称不同，追加历史
    if new["nickname"] != existing["nickname"]:
        existing["history"] = existing.get("history", [])
        # 先后顺序：existing 是先扫到的（旧），new 是后扫到的（新）。
        # 旧写法把 from/to 写反了，任何「按历史还原昵称」的功能都会取到反向结果。
        existing["history"].append({
            "field": "nickname",
            "from": existing["nickname"],
            "to": new["nickname"],
            "time": new["stats"]["latest"] or new["stats"]["earliest"] or "",
        })
    # 合并文件夹引用（保留多个文件夹路径）
    existing["folders"] = existing.get("folders", [existing["folder"]])
    if new["folder"] not in existing["folders"]:
        existing["folders"].append(new["folder"])
    # 合并作品
    existing["works"] = existing.get("works", []) + new["works"]
    # 去重作品（按日期+描述）
    seen = set()
    dedup = []
    for w in existing["works"]:
        key = (w["date"], w["desc"])
        if key not in seen:
            seen.add(key)
            dedup.append(w)
    existing["works"] = dedup
    existing["works"].sort(key=lambda w: w["date"], reverse=True)
    # 重新统计
    vc = sum(1 for w in existing["works"] if w["media_type"] == "video")
    ic = sum(1 for w in existing["works"] if w["media_type"] in ("image_single", "image_multi"))
    dates = [w["date"] for w in existing["works"] if w["date"]]
    existing["stats"] = {
        "video_count": vc,
        "image_count": ic,
        "work_count": len(existing["works"]),
        "earliest": min(dates) if dates else "",
        "latest": max(dates) if dates else "",
    }
    # 昵称：保留作品最新那个作为当前昵称（较合理的做法是保留较新的）
    # 简单策略：保留 existing 的 nickname（先扫描到的），后续可手动改
    return existing


# ============ 增量扫描 ============
# 思路：**一个作者文件夹的内容变了，它的目录 mtime 一定变**（新建/删除/改名文件都会动
# 目录 mtime）。所以根目录 scandir 一次，就能以 1700 个 DirEntry 的 mtime 判断「谁变了」——
# Windows 上 DirEntry.stat() 直接读目录枚举返回的缓存，不额外读盘，一次 listdir 就全拿到。
# 只有 mtime 变了的文件夹才真去列文件（那次才需要读盘）。
# 这样「新下了几个作者的作品」= 只扫那几个文件夹，其它 1690 个直接复用上次结果。
MTIME_CACHE = os.path.join(paths.DATA_DIR, "scan_mtimes.json")
# 这些字段不是从文件名/目录名推出来的，是靠接口/手工补的，重扫一个文件夹绝不能丢
KEEP_FIELDS = ("sec_uid", "douyin_id", "signature", "cover")


def _load_mtimes():
    try:
        with open(MTIME_CACHE, "r", encoding="utf-8") as fp:
            d = json.load(fp)
    except (OSError, ValueError):
        return {}
    folders = d.get("folders") if isinstance(d, dict) else None
    if not isinstance(folders, dict) or (isinstance(d, dict) and d.get("root") not in (None, SCAN_ROOT)):
        return {}
    return folders


def _save_mtimes(folders):
    """原子写 mtime 缓存；失败不影响本次扫描结果，只是下次又全量。"""
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        tmp = MTIME_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            json.dump({"version": 1, "root": SCAN_ROOT, "folders": folders}, fp, ensure_ascii=False)
        os.replace(tmp, MTIME_CACHE)
    except OSError as e:
        log("mtime 缓存写入失败（不影响结果）: %s" % e)


def scan_root_folders():
    """一次 scandir 拿到根目录下所有作者文件夹的 mtime_ns（不额外读盘）。"""
    out = {}
    try:
        it = os.scandir(SCAN_ROOT)
    except OSError as e:
        log(f"错误: 无法读取目录 {e}")
        return out
    with it:
        for e in it:
            if not UID_FOLDER_RE.match(e.name):
                continue
            try:
                if not e.is_dir():
                    continue
                out[e.name] = e.stat().st_mtime_ns
            except OSError:
                continue
    return out


def _load_old_accounts():
    """上一次的 data.json（增量扫描的复用来源）。坏了就当没有，走全量。"""
    try:
        with open(OUTPUT_JSON, "r", encoding="utf-8") as fp:
            d = json.load(fp)
    except (OSError, ValueError):
        return {}
    acc = d.get("accounts") if isinstance(d, dict) else None
    return acc if isinstance(acc, dict) else {}


def works_of_folder(acc, folder):
    """从（可能已合并过的）账号里只挑出属于这个文件夹的作品。

    作品里的 rel 一定以「文件夹名/」开头，所以能精确切回来。这一步是「复用」能用的前提：
    一个 uid 有过两个文件夹（改过昵称）时，各自复用自己那部分，不会把另一份重复算进来。
    """
    pre = folder + "/"
    out = []
    for w in (acc.get("works") or []):
        fs = w.get("files") or []
        if fs and all(str(f.get("rel", "")).startswith(pre) for f in fs):
            out.append(w)
    return out


def recalc_stats(works):
    vc = sum(1 for w in works if w.get("media_type") == "video")
    ic = sum(1 for w in works if w.get("media_type") in ("image_single", "image_multi"))
    dates = [w["date"] for w in works if w.get("date")]
    return {
        "video_count": vc,
        "image_count": ic,
        "work_count": len(works),
        "earliest": min(dates) if dates else "",
        "latest": max(dates) if dates else "",
    }


def clone_folder_account(old, folder):
    """把 old 里属于 folder 的那部分还原成一个「刚扫完这个文件夹」的账号 dict。"""
    works = works_of_folder(old, folder)
    acc = {k: v for k, v in old.items() if k not in ("works", "stats", "folders")}
    acc["folder"] = folder
    acc["folder_rel"] = folder
    acc["folders"] = [folder]
    acc["works"] = works
    acc["stats"] = recalc_stats(works)
    return acc


def keep_old_fields(new, old):
    """重扫出来的账号缺 sec_uid/抖音号/简介/封面时，用旧的补上（重扫只认文件名）。"""
    for k in KEEP_FIELDS:
        if not new.get(k) and old.get(k):
            new[k] = old[k]
    return new


def main():
    argv = sys.argv[1:]
    unknown = [a for a in argv if not a.startswith("-")]
    if unknown:
        log("忽略未知参数: %s（可用 --full 强制全量）" % " ".join(unknown))
    full = ("--full" in argv) or ("-f" in argv)
    log("开始扫描抖音下载目录...")
    log(f"扫描根目录: {SCAN_ROOT}")

    if not os.path.isdir(SCAN_ROOT):
        log(f"错误: 目录不存在 {SCAN_ROOT}")
        sys.exit(1)

    # 归一：同一个 uid 只能有一个文件夹（见 folder_merge.py）
    # 下载器给作者建目录用的是 `UID{uid}_{当前昵称}_发布作品`，昵称一改（最常见是加 emoji）
    # 下次下载就按新名字另建一个，旧作品留在原地、界面上看不见。必须在扫描之前做：
    # 先把磁盘理顺，这一趟扫出来的才是合并后的结果，否则又要等下一轮才对齐。
    phase("合并同作者文件夹")
    try:
        import folder_merge
        folder_merge.auto_merge(log=log)
    except Exception as e:  # noqa: BLE001
        # 合并失败绝不能挡住扫描 —— 大不了这轮还是老样子
        log(f"合并同作者文件夹出错（继续扫描）: {e}")

    # 提取 sec_uid 映射
    phase("提取 sec_uid 映射")
    log("提取 sec_uid 映射 (Data/*.xlsx)...")
    uid_to_sec, uid_meta = extract_sec_uid_from_xlsx()
    log(f"sec_uid 映射: {len(uid_to_sec)} 个")

    # 扫描作者文件夹（默认增量：只重扫 mtime 变了的文件夹）
    phase("扫描作者文件夹")
    t_all = time.time()
    folder_mtimes = scan_root_folders()
    names = sorted(folder_mtimes)
    total = len(names)
    log(f"作者文件夹: {total} 个（一次 scandir 拿到全部 mtime，不逐个读盘）")

    old_accounts = {}
    old_mtimes = {}
    if full:
        log("--full：强制把所有文件夹重扫一遍")
    else:
        old_accounts = _load_old_accounts()
        old_mtimes = _load_mtimes()
        if not old_mtimes:
            log("没有 mtime 缓存（这趟只能全扫一遍建缓存，之后就只有变化的重扫）")

    # 文件夹 → 上一次的账号（复用来源）
    by_folder = {}
    for _a in old_accounts.values():
        if not isinstance(_a, dict):
            continue
        for _f in (_a.get("folders") or [_a.get("folder")]):
            if _f:
                by_folder[_f] = _a

    accounts = {}
    reuse = 0
    rescan = 0
    progress(0, total)
    for i, name in enumerate(names, 1):
        old = by_folder.get(name)
        acc = None
        if old_mtimes.get(name) == folder_mtimes[name] and old is not None:
            acc = clone_folder_account(old, name)     # 没变：直接复用，一个文件都不用读
            reuse += 1
        if acc is None:
            acc = scan_author_folder(name)
            rescan += 1
            if old is not None:
                keep_old_fields(acc, old)
        if acc:
            # 下载记录库：把这个文件夹当前的文件（带 mtime）记进 data/download_log.db。
            # 只有重扫过的文件夹才写；复用的那些上次已经记过了。
            raw = acc.pop("_raw_files", None)
            if raw is not None:
                try:
                    download_db.record_folder(acc["uid"], name, raw)
                except Exception as e:
                    log(f"[下载记录] {name} 写入失败（不影响索引）: {e}")
            if acc.get("uid") in uid_to_sec:
                acc["sec_uid"] = uid_to_sec[acc["uid"]]
            uid = acc["uid"]
            if uid in accounts:
                # 同一 UID 出现在多个文件夹 = 账号改过昵称，合并并记录昵称历史
                merge_accounts(accounts[uid], acc)
            else:
                accounts[uid] = acc
        if i % 25 == 0 or i == total:
            progress(i, total)
            if i % 200 == 0 or i == total:
                log(f"进度 {i}/{total}（复用 {reuse} / 重扫 {rescan}）")

    if uid_meta:
        _remote_meta_save(uid_meta)

    dropped = [u for u in old_accounts if u not in accounts]
    log(f"文件夹：复用 {reuse} 个 / 重扫 {rescan} 个；作者 {len(accounts)} 位")
    if dropped:
        log(f"文件夹已消失、本次移除的作者 {len(dropped)} 位: "
            + ", ".join(dropped[:8]) + ("..." if len(dropped) > 8 else ""))

    data = {
        "meta": {
            "scan_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "total_authors": len(accounts),
            "root": SCAN_ROOT.replace("\\", "/"),
        },
        "accounts": accounts,
    }

    # 没有变化就不重写 73MB 的 data.json（写一次要一两秒，纯属浪费）
    changed = bool(full or rescan or dropped or not os.path.exists(OUTPUT_JSON))
    if changed:
        _write_data_json(data)
    else:
        log("没有任何文件夹变化 → data.json 不重写")

    _save_mtimes({n: folder_mtimes[n] for n in names})
    log(f"完成，用时 {time.time() - t_all:.1f} 秒")


def _write_data_json(data):
    # 原子写：先写临时文件再 os.replace，避免写一半崩溃留下半截 62MB JSON
    phase("写入 data.json")
    log(f"写入 {OUTPUT_JSON}...")
    tmp_path = OUTPUT_JSON + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, OUTPUT_JSON)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    size_mb = os.path.getsize(OUTPUT_JSON) / 1024 / 1024
    log(f"完成! data.json 大小 {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
