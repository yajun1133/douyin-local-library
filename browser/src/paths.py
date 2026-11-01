#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全项目唯一的路径来源。

目录约定（根 = browser/）：

    src/            服务端与核心脚本；前端在 src/web/
    data/           data.json / user_data.json / count_cache.json / csv
    cache/           thumb_cache / trans_cache / cover_cache
    backup/ export/ docs/
    start-browser.bat    clean-cache.bat

为什么集中到这里：以前每个脚本各写一遍 `os.path.dirname(__file__)`，
代码一挪目录，数据路径就跟着变，重启后读不到 user_data.json 会退化成空库
（历史丢数据事故的一种形态）。现在只在这里定义一次，脚本一律 `from paths import ...`。
"""

import os

# ============ 目录 ============
SRC_DIR = os.path.dirname(os.path.abspath(__file__))      # .../browser/src
APP_ROOT = os.path.dirname(SRC_DIR)                        # .../browser
WEB_DIR = os.path.join(SRC_DIR, "web")                     # index.html / app.js / style.css
DATA_DIR = os.path.join(APP_ROOT, "data")
CACHE_DIR = os.path.join(APP_ROOT, "cache")
BACKUP_DIR = os.path.join(APP_ROOT, "backup")
EXPORT_DIR = os.path.join(APP_ROOT, "export")
DOC_DIR = os.path.join(APP_ROOT, "docs")
# 回收站：网页里「删除作品」不再直接抹掉文件，而是整份搬到这里（保留原目录结构），
# 随时可以「恢复」搬回原处。用户明确要求：先放回收站，确认不要了再彻底删。
RECYCLE_DIR = os.path.join(APP_ROOT, "recycle-bin")
# 标注台的意见存这里（不给用户看，是给改代码的人/AI 看的）
ANNOTATE_DIR = os.path.join(DATA_DIR, "annotations")

# ============ 数据文件 ============
USER_DATA = os.path.join(DATA_DIR, "user_data.json")        # 用户标注，唯一不可再生的数据
DATA_JSON = os.path.join(DATA_DIR, "data.json")             # 扫描产物，可随时重生成
COUNT_CACHE_PATH = os.path.join(DATA_DIR, "count_cache.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "黑名单_不下载.csv")
UID_MAP_CSV = os.path.join(DATA_DIR, "uid_secuid_对照表.csv")
BATCH_RESULT_PATH = os.path.join(DATA_DIR, "batch_result.json")

# ============ 缓存目录 ============
THUMB_CACHE = os.path.join(CACHE_DIR, "thumb_cache")
TRANS_CACHE = os.path.join(CACHE_DIR, "trans_cache")
# 封面图单独一个目录：**永不淘汰、永不清空**（用户明确要求）。
COVER_CACHE = os.path.join(CACHE_DIR, "cover_cache")

# ============ 脚本 ============
SCANNER_PY = os.path.join(SRC_DIR, "scanner.py")
HELPER_QUERY_UID = os.path.join(SRC_DIR, "query_uid_by_secuid.py")
HELPER_PROFILE = os.path.join(SRC_DIR, "profile_helper.py")   # 常驻：登录态查资料（3.12）
HELPER_FAVORITES = os.path.join(SRC_DIR, "fetch_favorites.py")  # 抓收藏/喜欢（3.12）

# ============ 外部路径（换机器只需要改这一段） ============
MEDIA_BASE = r"E:\dyyyyyyyyyyyyydownload\TikTokDownloader_V5.6_Windows_X64"
SCAN_ROOT = os.path.join(MEDIA_BASE, "downnnnnnnn")
PYTHON = r"C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
PYTHON312 = r"C:\Python312\python.exe"                   # 下载器 curl_cffi 只支持 3.12
FFMPEG = r"D:\app\ffmpeg-n8.1-latest-win64-lgpl-8.1\ffmpeg-n8.1-latest-win64-lgpl-8.1\bin\ffmpeg.exe"
FFPROBE = FFMPEG.replace("ffmpeg.exe", "ffprobe.exe")

# 下载器目录：curl_cffi（3.12 编译版）+ ABogus 签名都在这。抓收藏/喜欢、查 uid 要用。
DOWNLOADER_DIR = os.path.join(MEDIA_BASE, "DouK-Downloader_V5.8_Windows_X64")
DOWNLOADER_LIB = os.path.join(DOWNLOADER_DIR, "lib")
DOWNLOADER_SETTINGS = os.path.join(DOWNLOADER_DIR, "Volume", "settings.json")

# 用户可配置的设置文件（数据库/FFmpeg/组件地址、更新频率禁用状态等）
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# 服务端口（restore_backup.py 等服务存活检测也用它）
PORT = 8723


def ensure_dirs():
    """建齐运行期需要的目录。启动时调用一次，省掉下面各处 os.makedirs。"""
    for d in (DATA_DIR, CACHE_DIR, BACKUP_DIR, EXPORT_DIR, RECYCLE_DIR, ANNOTATE_DIR,
              THUMB_CACHE, TRANS_CACHE, COVER_CACHE):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass


if __name__ == "__main__":
    # python src/paths.py —— 打印当前生效的路径，排查「文件到底读的是哪份」
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for k in ("APP_ROOT", "SRC_DIR", "WEB_DIR", "DATA_DIR", "CACHE_DIR",
              "BACKUP_DIR", "EXPORT_DIR", "USER_DATA", "DATA_JSON", "SCAN_ROOT"):
        v = globals()[k]
        print("%-14s %s%s" % (k, v, "" if os.path.exists(v) else "   <缺失>"))
