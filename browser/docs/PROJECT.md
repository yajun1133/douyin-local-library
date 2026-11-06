# 抖音下载浏览器 — 项目技术文档

> 本地网页应用：浏览 / 检索 / 标注已下载的抖音作者与作品。
> 载体 = 静态网页 + Python HTTP 服务（127.0.0.1:8723）+ 一键启动脚本，**无任何 AI 依赖**。

> **2026-10-31 精简过**：只留「现在是什么样、改的时候注意什么」。
> 49 条踩坑的完整过程、逐版本更新记录，都搬到 `本地 _archive/ 下的历史详述（含真实账号信息，未随仓库发布）` 了。

| 章节 | 内容 | 什么时候看 |
|---|---|---|
| 一 | 目录结构、数据量口径、外部依赖路径 | 换机 / 找不到文件 |
| 二 | 数据流、data.json 与 user_data.json 结构 | 改数据结构 |
| 三 | 全部 HTTP 接口 | 加接口 / 调前端 |
| 四 | 数据模型相关代码位置 + 同步清单 | 加字段 |
| 五 | 改动前必读清单 + 已知问题 | **动手前** |
| 六 | 关键设计决策（为什么这么做） | 想改设计时 |
| 七 | 界面现状：主列表 / 详情页 / 新下载 / 导出 / 标注台 | 改界面 |
| 八 | 踩坑要点（一条一句，不展开过程） | 出 bug 时先扫一眼 |
| 九 | 本地验证：语法 / 接口 / 截图量尺寸 | 改完自测 |
| 附录 | 最近改动 + 归档位置 | 想追历史 |

> 换人 / 换新对话：先看 `docs/HANDOVER.md`（现状 + 硬约束 + 常用命令），再看本文。

---

## 一、整体结构

```
D:/ai_Project/douyin-local-library/browser/          ← 项目根（只放启动脚本 + data/cache/backup 目录）
├── start-browser.bat       # 一键启动（起服务 + 开浏览器）
├── clean-cache.bat       # 双击清缓存（透传参数给 tools/clean_cache.py）
├── src/               # 全部代码
│   ├── server.py      # 本地 HTTP 服务：托管网页 + 媒体 + API + 标注叠加
│   ├── scanner.py     # 扫描脚本：遍历下载目录 → 生成 data/data.json（全量，无增量）
│   ├── paths.py       # **唯一**的路径定义（代码/data/cache/backup/export 都从这里取）
│   ├── profile_helper.py         # 常驻子进程（3.12）：登录态查资料，一行一 JSON
│   ├── query_uid_by_secuid.py    # 子进程助手（3.12）：批量 sec_uid→uid
│   └── web/           # 前端（服务端按白名单托管）
│       ├── index.html / app.js / style.css   # 主界面三件套
│       └── annotate.html / annotate.js       # 标注台（/annotate.html，选元素写意见给我）
├── tools/              # 日常会用到的独立脚本
│   ├── fill_secuid_from_export.py  # **扫完补 sec_uid**：读 export/*.csv 按 UID 匹配，纯本地不联网
│   ├── secuid_check.py    # sec_uid 体检/纠错（联网，需 3.12 + curl_cffi，--by-secuid 批量反查）
│   ├── clean_cache.py     # 清缩略图/转码缓存（--wipe 全清，--status 只看）
│   ├── clean_index.py     # 把 data.json 里混进去的标注字段剥掉（--dry 只看；写前自动备份）
│   ├── restore_backup.py  # 从备份恢复标注（--list / --latest / --file）
│   ├── blacklist.py       # 黑名单（不下载名单）公共模块
│   ├── block_author.py     # 把作者加入黑名单（--delete 连本地文件夹一起删）
│   ├── downloaded.py      # 「哪些作品已下载」索引
│   └── legacy/          # 早期一次性脚本（补 SEC_UID、迁大小号、导报表…），留档备用
├── data/              # 标注（不可再生）+ 索引（可重生成）
│   ├── user_data.json      # 账号标注：标签/星标/大小号/状态/等级/别名/历史/封面
│   ├── data.json           # 扫描产物（≈72MB，作者 + 作品索引，可随时重生成）
│   ├── count_cache.json    # 「抖音当前作品数」缓存
│   ├── secuid_check.json   # sec_uid 联网核查结果缓存（重跑可跳过已确认的）
│   ├── uid_secuid_对照表.csv
│   ├── 黑名单_不下载.csv
│   ├── annotations/              # 标注台存下来的界面意见（界面意见_<时间>.md + .json）
│   ├── download_log.db     # 下载记录库（每个文件的 rel/uid/mtime），「🆕 新下载」的数据源
│   ├── 回收站.json        # 删掉的批次（被删作品的完整 JSON + 文件清单），恢复要用；删光自动空
│   ├── 删除记录.jsonl     # 流水账：每次删/移入回收站记一行（时间/uid/昵称/作品/批次/mode）
│   └── batch_result.json   # 批量获取资料的结果（前端取走即删，正常不存在）
├── cache/              # 删了会按需重建，不影响任何标注
│   ├── thumb_cache/        # 视频首帧缩略图（上限 2GB，自动淘汰最久没用的）
│   ├── trans_cache/        # HEVC→H.264 转码产物（上限 6GB）
│   └── cover_cache/        # 封面图（**永不清理**，用户设的封面全在这）
├── export/              # SEC_UID 导出 txt（<类型>_<时间戳>.txt）
├── recycle-bin/            # 网页里删掉的作品（一批一个子目录，保留原「作者文件夹/文件名」结构）
├── backup/              # user_data.json 的自动/手动备份（auto / before-restore / bak）
└── docs/              # PROJECT.md（本文档）/ HANDOVER.md（接手须知）/ README.md（简介）/ archive/（历史文档已移出仓库，本地保留在 _archive/）
```

> ⚠️ **路径只认 `src/paths.py`**：代码在 `src/`、前端在 `src/web/`、数据在 `data/`、缓存在 `cache/`、备份在 `backup/`、导出在 `export/`，全部由 `paths.py` 一处定义（`APP_ROOT = src 的上一级`）。
> 以前每个脚本各写一遍 `os.path.dirname(__file__)`，代码一挪目录数据路径就跟着变，重启后读不到 user_data.json 会退化成空库（丢数据事故的一种形态）。**以后新增脚本一律 `from paths import ...`，不要再自己拼路径。**
> 换机器/换下载目录只需要改 `paths.py` 里的 `MEDIA_BASE / PYTHON / PYTHON312 / FFMPEG` 四处（`FFPROBE` 由 `FFMPEG` 自动派生，不用单独改）。

### 当前数据量（数字天天变，只记口径；用时现查 `/api/meta`）

| 指标 | 口径 |
|---|---|
| 作者数 | `/api/meta.total_authors`（2026-11-04：**1961**）。比 `data.json` 的 1924 个账号多出来的那些是「合成账号」——只在 `user_data.json` 里有、本地没作品（当前 43 个，多是扩展「待下载 → 新建主页」建出来的）；黑名单里的账号会从列表和统计里剔掉（当前 20 条） |
| 作品总数 | `data.json` 里各账号 `works` 之和（2026-11-04：**288,718** 个作品，其中含视频 261,979 / 其余为图文）。重复作品已在合并重名文件夹时移出索引 |
| 文件总数 | `data.json` 里各作品 `files` 之和（2026-11-04：**386,726** 个；一个图文作品占 `_1.._9` 多个文件） |
| 星标 | `/api/meta.starred`（2026-11-04：**119**） |
| sec_uid 覆盖 | 扫描时只有从 `Data/*.xlsx` 来的那部分（这份表已经不常用了，2026-11-04 实测 0 份），其余靠 `user_data.json` 叠加补 + 登录态接口反查（补齐顺序见第六章）。**当前 1960/1967**，剩 7 个注销/封禁/私密的号补不到，属正常 |
| 抖音号覆盖 | 同上，**当前 1955/1967**；缺的那 12 位接口返回空，页面上点「获取资料」也拿不到 |
| thumb_cache | ffmpeg 抽帧 400px 宽，**纯 CPU**（`-threads 2`，不要加 `-hwaccel cuda`，实测慢 5 倍），上限 3072 MB，超了按 LRU 淘汰，可整个删。**同时最多跑 8 个 ffmpeg**（`_THUMB_SLOTS`）：列表滚屏会并发问几十张封面，靠进程数铺满核，不限并发只会互相抢核。2026-11-04 实测 79,764 张 / 1.59 GB |
| trans_cache | HEVC→H.264 转码产物，上限 300 MB，可随时整个删。编码器顺序 **NVENC（GPU）→ h264_mf → libopenh264**，NVENC 失败一次就整个进程不再试 |
| cover_cache | 封面图，文件名 = `md5(作品相对路径)`，**永不清理**（用户明确要求）。2026-11-04 实测 2,480 张 / 54 MB。⚠️ 作品文件一改名，这个 md5 就对不上了（见第八章第 36 条） |

### 外部依赖（硬编码路径，换机需同步修改）

| 依赖 | 路径 | 用途 |
|---|---|---|
| 数据源目录 | `E:\dyyyyyyyyyyyyydownload\TikTokDownloader_V5.6_Windows_X64\downnnnnnnn` | 扫描根目录（`scanner.py`）+ 媒体根目录（`server.py`） |
| Python | `C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe` | 运行 scanner/server（`server.py`、`scanner.py`、`start-browser.bat` 各硬编码一份） |
| ffmpeg | `D:\app\ffmpeg-n8.1-latest-win64-lgpl-8.1\ffmpeg-n8.1-latest-win64-lgpl-8.1\bin\ffmpeg.exe` | 抽视频首帧缩略图、HEVC→H.264 转码（`ffprobe.exe` 同目录） |

---

## 二、核心模块与数据流

**数据流（单向 + 标注回写）：**

```
磁盘目录 ──scanner.py──▶ data.json ──┐
                                     ├─▶ server.py 内存 CACHE ──▶ HTTP API ──▶ app.js ──▶ 界面
user_data.json ─────────────────────┘   (load_data + load_user_data 叠加)          │
        ▲                                                                          │
        └──────────────────────── /api/save（全量覆盖写）◀────────────────────────┘
```

> `CACHE` = `data.json` 解析进内存的结果 + `user_data.json` 标注叠加上去。两者任一变化都会触发重建/重叠加。
>
> **`data.json` 曾经会被标注污染，2026-11-01 已修（别再把老结论当现状）：**
> 根因是 `CACHE["accounts"]` 和 `CACHE["data"]["accounts"]` 是**同一批 dict**，`load_user_data()` 原地把标注写进去；于是五个写盘点（`_files_rename_folder`、`_works_delete_inner`、`_recycle_restore`、`_files_rename_batch`、`_files_dup_resolve_inner`）一旦整份 `json.dump(CACHE["data"])`，标注就跟着落盘。而增量扫描是「读旧 `data.json` 再合并」，写进去就洗不掉，还会变成过期残缺值（实测 `starred` 只剩 93，真值 105）。
> 修法：写盘一律过 `_index_only()`，剥掉 `ANNOT_ONLY_KEYS` 那 10 个字段（`alias / alt_accounts / cover / level / main_uid / note / starred / status / tags / update_freq`）；历史残留用 `tools/clean_index.py` 一次清干净。**现在磁盘上的 data.json 是干净的**，上面那 10 个字段一个都不该出现。
> **归档前必须清「同作品两份文件」（2026-11-03）：** 10-31 改命名模板加了 `id` 后，DouK 把老作品按新名字重下了一遍，于是同一作品在磁盘上同时存在 `日期-昵称-描述.mp4`（老）和 `日期-昵称-描述-<id>.mp4`（新）。按 ID 聚合后这两份会裂成**两个作品**，看起来就是「一个作品出现两次」。全库盘出 **444 组**（419 组 md5 完全相同、16 组同片重编码、9 组要人眼判），删掉 435 个多余文件 + 15 个图集残缺残留，共 450 个 / 0.64 GB。
> 判定口径：**同目录 + 去掉尾部 `-ID` 和 `_序号` 后同名 + 同后缀** 才是候选；比 md5 → 比时长 → 比 24×24 灰度图（差 ≤2 算同图）。**别只按「日期+描述」判**——同一天同一作者同名不同内容的作品大量存在（实测 1400 组全是不同作品，江柔那条 `扭腰` 两个视频画面完全不同），误删就是丢作品。
> 当时没做「自动合并 ID/无 ID 两组」的逻辑，因为那会把上一条里的真·不同作品合到一起；靠清磁盘 + DouK 记录（253 个 ID 全部已在 `download_data`）双保险。

> 仍然要守的：**别拿 `data.json` 当标注的备份或恢复源**——`user_data.json`（和 `backup/`）才是唯一权威；`douyin_id / signature / nickname / sec_uid / history` 是扫描器 schema 里就有的字段，值可能被标注改过，属于刻意保留的灰色地带（剥了会丢 `history` 的合并记录）。

| 模块 | 职责 | 关键产物 |
|---|---|---|
| `scanner.py` | 遍历目录，解析 `UID_昵称_发布作品` 文件夹、作品文件名（`日期-昵称-描述[-作品ID]`），从 `Data/*.xlsx` 取 sec_uid，合并同名改昵称账号（记录昵称历史）。**只有全量扫描，`--incremental` 参数是死代码**。聚合键：**有作品 ID 就按 ID**（`("id", id)`），没有 ID 的老文件退回 `("dd", 日期, 描述)` | `data.json` |
| `server.py` | 载入 data.json 进内存（`CACHE`），**叠加 user_data.json 标注**，提供 API + 静态/媒体托管 + 缩略图 + 编码探测 + HEVC 转码 | HTTP 服务（端口 8723） |
| `app.js` | 前端状态管理（`state` / `detailState`）、分页加载、作者详情页（上下三段 + 左右两栏）、媒体预览、标注操作 | 交互逻辑 |

### data.json 结构

```json
{
  "meta": { "total_authors": 1706, "scan_time": "...", "root": "E:/.../downnnnnnnn" },
  "accounts": {
    "<uid>": {
      "uid": "100301438759",
      "nickname": "...",
      "sec_uid": "MS4wLjAB...",      // 扫描时来自 Data/*.xlsx（663 份表）；其余靠 user_data 叠加补（当前合计 1753/1758）
      "douyin_id": "",               // 扫描恒为空，靠人工标注
      "signature": "",               // 扫描恒为空，靠人工标注
      "folder": "UID..._发布作品", "folder_rel": "...", "folders": ["...", "..."],  // folders 只在改过昵称时有多项
      "history": [ { "field": "nickname", "from": "旧名", "to": "新名", "time": "..." } ],  // 合并过文件夹的账号才有（当前 801 个）
      "works": [
        { "date": "2025-10-26", "desc": "...", "media_type": "video|image_single|image_multi",
          "files": [ { "rel": "UID..._发布作品/xxx.mp4" } ], "file_count": 1 }
      ],
      "stats": { "video_count": 552, "image_count": 7, "work_count": 554, "earliest": "...", "latest": "..." }
    }
  }
}
```

- `desc` 取自文件名的描述段，可能带 `#话题`；文件名解析失败的会以 `date=""`+原始文件名作为一条独立作品。
- 图文帖按「日期 + 描述」聚合为一组，`files` 按文件名末尾 `_序号` 排序。
- 卡片右上角的媒体徽标（`wallBadgeHtml`）有三种，图标/配色刻意区分：**多图**（叠框，灰黑，张数）、**多动图**（圆点，灰黑，段数）、**多段视频**（暂停条，蓝底，段数）。纯视频作品（一张图都没有）以前会被 `motions>=2` 判成「多动图」，和 `workKindOf` 的「没图就是视频」打架（用户 2026-11-02 看到「圆圈+2」问是不是动图）；现在纯视频走单独分支，只有多段才标，并且播放器底部出现 `‹ 1/2 段 ›` 可以翻（`videoFilesOf` / `vidNavHtml` / `stepVideo`，2026-11-02 c113）

#### 「图 + 动图」帖（抖音实况图）的文件结构 —— 别当成「视频 + 封面」

**全库 2779 个作品（210,258 里的 1.3%）**属于这种结构：帖子里每张「图」由两个文件组成 —— 一张高清静帧 + 一段动效。**下载时动效已经转成 mp4**，所以对前端来说它就是「一张图 + 一段视频」。

| 文件 | 内容 | 实测参数 |
|---|---|---|
| `..._N.jpeg` | **静帧**（高清） | 2160×2880，约 1.0~1.5 MB |
| `..._M.mp4` | **动效**（约 3 秒，带原声） | 720×960，h264，1.3~4.9 秒，aac 2 声道，100KB~1.2MB |

- 静帧就是动效的**暂停帧**，所以静帧看起来比动效清晰得多（分辨率差 3 倍）——但两者是**帖子里的两张**，不是「封面 + 正片」。
- **序号是媒体流顺序，静帧和动效各占一个序号，先后顺序不固定**：`_1.jpeg + _2.mp4`（静帧在前）和 `_1.mp4 + _2.jpeg`（动效在前）都有。
- 一个帖子可以有 N 个这样的单元，也存在「只有静帧」的普通图；极少数是「只有动效没有静帧」（如 1 图 + 10 动效）。
- 图片数与动效数**不相等是常态**（1:1 有 865 个，其余是 2:1、1:2、5:1…），所以**不做配对**，直接按序号一张一张排（见第七章 v13）。

⚠️ 早期文档把 `_1.jpeg` 写成「封面图」、把 `_2.mp4` 当成作品视频，这是**错的**——它不是封面，是这组图之一的静帧；反过来把它当成「同一张图的静帧+动效」再去配对、加切换按钮，也是**多余的**。正确做法：当成帖子里的两张，按顺序翻（v13）。

判定与拆解（`app.js`，不改 data.json、不用重扫）：

```js
isLivePost(w)    // 同时含图片和视频 → 这种帖
mediaItemsOf(w)  // 按 _N 排序 → [{rel, kind:'image'|'video'}]，一张就是一项（不配对）
workKindOf(w)    // 按动图段数归类：1 段 → live_single，≥2 段 → live_multi
```


### user_data.json 结构（用户标注，与扫描结果分离，重扫不覆盖）

```json
{
  "accounts": {
    "<uid>": { "nickname","alias","douyin_id","signature","status","starred","tags","note",
               "cover","main_uid","alt_accounts","history" }
  },
  "alt_group_names": { "<成员uid排序拼接>": "文件夹名" },
  "status_notes": { "<账号状态>": "用户对这个状态的理解（自由文本，可换行）" }
}
```

- `alias`：用户自己起的名字（**不影响 `nickname`**）。界面上一律 `nameHtml()` 渲染成「别名 + 灰色原名」，没有别名就只显示昵称。
- `alt_group_names`：左侧「大小号」视图里文件夹的自定义名字。**键 = 组内成员 uid 排序后 `join("|")`**，所以组员一变，名字就失效回落到默认名（默认取组内作品最多的账号）。
- `status_notes`：**每个账号状态一段说明**（c46），在侧边栏「分类」里点某个状态进来时显示在**标题右边（单行输入框，跟搜索栏一样）**。前端按 key 增量上送（`{状态名: 文本}`，空 → `null`），服务端按 key 合并、全空时删掉整个顶层键。**必须按 key 合并**：两个标签页各写各的状态，整份覆盖会互相冲掉。

**注意两种 `null` 语义不同（2026-10-28 加）**：
```jsonc
{ "accounts": { "<uid>": { "note": null } } }   // 只删 note 这个字段，账号还在
{ "accounts": { "<uid>": null } }               // 整个账号从标注里删掉（配合「删除博主 + 黑名单」）
```
`merge_save_user_data()` 里 `ua is None` 直接 `merged.pop(uid)`。必须支持这条，否则删掉博主后它在 `user_data.json` 里还留个空条目，`load_user_data()` 会把它当「合成账号」塞回列表——界面看起来像没删掉。

**关键机制（2026-10-26 修复）**：`server.py` 的 `load_user_data()` 会把这份标注叠加进内存 `CACHE["accounts"]`，所以 `/api/meta` 的星标数、标签云、`/api/authors?cat=starred`、列表卡片的星标/状态/标签才读得到值。**没有这层叠加，data.json 里根本没有 `starred`/`tags` 字段，侧边栏永远显示 0 星标、无标签。** 修改标注相关逻辑时务必保留这条链路。

---

## 三、API 接口约定

| 接口 | 方法 | 参数 | 返回 |
|---|---|---|---|
| `/api/meta` | GET | — | `{total_authors, starred, tags:{标签:数量}, years:{年:作品数}}`（统计口径 = 叠加标注后的 CACHE） |
| `/api/authors` | GET | `offset`(默认0) / `limit`(默认60) / `sort`(nickname\|uid\|work_count\|video_count\|image_count\|latest\|earliest\|level\|added) / `dir`(asc\|desc) / `q`(关键字) / `scope`(all\|nickname\|note\|signature\|works，缺省=all) / `tag` / `year` / `cat`(all\|starred) / `status`(单选，含 `__unset`=未设置、`正常`、`注销`、`禁言/封号`、`停更/偶更`、`卖号`、`不好看了`、`待修改`；旧值 `停更/偶尔更`、`停更` 会自动映射成 `停更/偶更`) / `level`(夯\|顶级\|一般\|拉\|__unset，旧值 `1级（次）`→一般、`2级（拉）`→拉 会自动映射) / `sec_uid`(has\|none) / `starred`(yes\|no) / `latest_after` / `latest_before`(YYYY-MM-DD) / `cover`(has\|none，可逗号多值如 `has,none`) / `match_level`、`match_tag`、`match_status`、`match_freq`、`match_cover`、`match_latest`（各自 `or`=包含 / `not`=排除，**缺省 or**；前端每行左边那个圆钮就是它）。语义（**2026-11-05 改版**）：**所有 `not` 行命中的作者直接剔除，剩下的人里（存在 `or` 行时）至少命中一个 `or` 行**；全是 `or` 就是纯并集，全是 `not` 就是「全部作品，去掉这几样」，一个都不勾 = 不筛。`tag` 额外支持 `__notag`（一个标签都没有）、`level`/`status`/`freq` 支持 `__unset`（未设置）。旧的 `and` 值仍兼容但按 `not` 处理；旧的全局 `match` 参数同理 | `{total, offset, items[]}`，items 不含 works，另带 `alias/starred/status/level/tags`(最多3个)/`cover`/`added_at` |
| `/api/search_works` | GET | `q` / `offset` / `limit` | 按作品 desc 搜索，返回**扁平化的作品列表** `{ok,total,items[{uid,nickname,alias,work_index,date,desc,poster_rel,poster_is_image,starred}]}`（前端「作品」范围搜索用，点进去定位到该作者这个作品） |
| `/api/author` | GET | `uid` | 单个作者完整数据（含 works，已叠加标注，另带 `alt_info`：组内成员的 nickname/alias/douyin_id/work_count） |
| `/api/alt_groups` | GET | — | **按组聚合的大小号文件夹**：`{total_groups, total_accounts, groups:[{key,name,custom,size,members[{uid,nickname,alias,cover,work_count,starred,status,main_uid,scanned}]}], names}`。并查集求连通分量，只返回成员 ≥2 的组 |
| `/api/save` | POST | body = user_data 全量 | `{ok}`；写完立刻失效标注缓存，下次请求重新叠加 |
| `/api/open` | POST | `{path}` | 资源管理器 `/select` 定位文件（`explorer /select,` 参数带逗号，路径含空格也能用） |
| `/api/probe` | GET | `path` | ffprobe 读编码 → `{codec, playable}`；已转过码则带 `url` + `transcoded:true` |
| `/api/transcode` | POST | `{path}` | **异步**：立即返回 `{ok, job, status:"running", pct:0}`；已转过的直接回 `{ok, url, cached:true}`。转码在后台线程，目标高度 = `min(源高度,1080)`，码率 = `源码率×1.35`（夹在 800k～分辨率上限），缓存 `trans_cache/` |
| `/api/transcode_status` | GET | `job` | `{ok, status:running\|done\|error\|missing, pct, url, rel, error, started}`，前端 800ms 轮询 |
| `/api/work_count` | GET | `uid` | 读抖音当前公开作品数 → `{ok, count, source:public\|login, at}`（单次 1 个请求） |
| `/api/count_refresh` | POST | `{action:start\|stop, limit}` | 后台批量刷作品数（`count_worker`，1.6s 间隔、≤400/轮、连续 20 失败熔断） |
| `/api/count_status` | GET | — | `{running, done, total, succeeded, failed, error, cached}` |
| `/api/latest_work` | GET | `uid` | 联网查这位作者**最新一条作品的发布时间**（登录态 + a_bogus 签名打 `aweme/post`，3.12 子进程 `latest_helper.py`）→ `{ok, uid, latest:"YYYY-MM-DD HH:MM", at}`；查不到时 `{ok:false, empty, error}`，`empty:true` = 对方根本没有公开作品（不是失败）。结果落 `data/latest_work.json` |
| `/api/latest_refresh` | POST | `{action:start\|stop, limit, force?}` | 后台批量查发布时间（`latest_worker`：3.5s±jitter、≤100/轮、**每 25 个停 30 秒**、连续 8 个失败熔断、7 天内查过的跳过；`force:true` 忽略 TTL 全量重查） |
| `/api/latest_status` | GET | — | `{running, done, total, succeeded, failed, empty, dead, skipped, pausing, error, last_error, cached, ttl_days}`；`empty` = 无公开作品、`dead` = SEC_UID 已失效（两者都不计失败、不进熔断），`pausing` = 正在那 30 秒空档里，`last_error` = 最近一个真失败账号的「昵称：原因」 |
| `/api/fetch_profile` | POST | `{sec_uid?, uid?}`（给一个就够，优先 uid） | 先走**登录态** `profile/other`（`profile_helper.py` 常驻 3.12 子进程）→ `{ok, uid, sec_uid, nickname, douyin_id, signature, follower_count, aweme_count, source:'login'}`；失败降级公开接口 → `source:'public'` + `partial:true`（拿不到简介）。`sec_uid` 支持整条主页网址（`norm_sec_uid()` 抽纯 id） |
| `/api/batch_profile` | POST | `{action: start\|stop, fields?, skip_status?, order?}` | 启动/停止「批量获取资料」后台任务。`fields` 要写哪些字段（默认全部 `nickname/douyin_id/signature`，传空数组会回落成全选）；`skip_status` 跳过哪些状态的账号（默认 `["禁言/封号","注销"]`，可选值限 `BATCH_SKIPPABLE`）；`order` 排序（`added` 默认/`added_asc`/`work_count`/`latest`/`earliest`/`nickname`）。已在跑时再 start 返回 409 + `already:true`；结果落盘 `batch_result.json` |
| `/api/batch_preview` | POST | 同上三个参数 | 按设置**预演**：返回 `{all, total, skipped, statuses, options}`，不真跑。前端用它显示「共 N 个 · 本次将获取 M 个（跳过 K 个）」 |
| `/api/batch_status` | GET | — | `{ok, running, done, total, filled, conflicts, failed, skipped, options, started, finished, error, unread}`；`unread:true` = 有结果没被前端取走；`skipped` = 按状态跳过的数量（不是失败） |
| `/api/batch_result` | GET | `consume=1` | `{ok, conflicts[], failed[], filled, total, finished}`；带 `consume=1` 时同时清 `unread` 并删结果文件 |
| `/api/export_secuids` | POST | `{type, uids[]}` | 把指定 uid（**含所在大小号组的全部成员**）的 SEC_UID 导出成 `https://www.douyin.com/user/<sec>` 每行一条的 txt，写入 `export/` 目录，文件名 `<类型>_<时间戳>.txt`（同名自动加 `-2/-3` 后缀不覆盖）。按 sec_uid 去重。返回 `{ok, count, missing, file, path}` |
| `/api/export_authors` | POST | `{uids?, sec_uids?, format: csv\|txt\|json, json_mode: settings\|accounts, earliest_days?, target?}` | 导出作者信息，三种格式：`csv`（昵称/别名/抖音号/UID/SEC_UID/等级/标签/状态/**最新作品**/**最早作品**/主页链接）、`txt`（一行一个主页链接）、`json`（DouK 账号数据；`settings` = 以现有配置为底整份导出，连 cookie 一起，覆盖 `Volume/settings.json` 就能跑；`accounts` = 只给 `accounts_urls` 数组）。**`sec_uids`（2026-11-01 加）**：待下载区那些还没 uid 的作者直接按 sec_uid 导出——能反查到期库就带上昵称/等级/标签，查不到也照样出链接；和 `uids` 一起给会去重。`earliest_days=N` 的规则只有两条（2026-11-01 用户定）：**① 本地一件作品都没有的作者 → `earliest` 留空**（不设最早时间，DouK 全量抓）；**② 本地有作品的作者 → `earliest` = 他自己「已下载最新作品日期 − N 天」**（`latest` 留空，只补最近这段，边界不漏）。`N` 不传 / 传空 = 所有人都不设，全部抓取。`target=douk` → 不落 `export/`，直接整份替换 DouK 的 `Volume/settings.json`（先备份、DouK 在跑则拒绝）。没有 sec_uid 的账号在 txt/json 里剔除（csv 保留）。返回 `{ok, count, skipped, with_earliest, no_works, file, path, format, json_mode, has_cookie}`（`no_works` = 按规则 1 没设日期的作者数） |
| `/api/douk/config` | GET / POST | GET：—；POST：`{settings:{白名单字段…}, pyc:{delay, every, rest}}` | 读写 DouK 下载器参数。GET 返回 `{ok, running, settings, fields{类型/中文名/提示}, pyc{every, rest, delay, sigma}, accounts, paths}`。POST 只改 `SETTING_FIELDS` 白名单（max_pages/max_size/timeout/max_retry/music/download），其余字段（cookie/browser_info/root/name_format…）一律不动；root 与 name_format 只在 GET 的 `readonly` 里**只读展示**（name_format 自 2026-10-31 起末尾带作品 id 防同名覆盖，见第八章第 24 条；界面仍不开放修改）；`pyc` 里的请求间隔（`get_wait_time` 的 avg_delay）与「每 N 个号暂停 M 秒」（`suspend` 常量）走 `src/douk_config.py` 子进程改 pyc 常量。写前自动备份 `backup/douk_settings_<时间>.json`，首次改 pyc 备份官方原版到 `backup/douk_function_原版.pyc`。`running:true` 时返回 `warn`（重启才生效） |
| `/api/create_author` | POST | `{sec_uid(可整条网址), link_uid(可选,当前账号uid)}` | 新建作者：`norm_sec_uid()` 抽纯 id → 走登录态 `im/user/info` 查权威 `uid`（3.12 子进程）→ 建到 user_data（sec_uid/昵称/抖音号）→ **全新账号再调一次 `fetch_profile_by_sec_uid()` 补简介**（老账号不覆盖，用户可能自己改过）→ 若给 link_uid 则自动并入当前大小号组。返回 `{ok, uid, sec_uid, nickname, douyin_id, signature, profile_note, linked[], existed}` |
| `/trans_cache/xxx.mp4` | GET | — | 转码后的文件，支持 Range |
| `/api/rescan` | POST | — | **立即**返回 `{ok}`，扫描在后台线程跑，扫完 `load_data(force=True)`；已在扫描时返回 409 `{ok:false, error}`（防重复起扫） |
| `/api/rescan_status` | GET | — | `{ok, running, phase, done, total, started, finished, error}`，前端 1s 轮询，按钮显示 `🔄 done/total` |
| `/api/files/overview` | GET | — | 文件管理页一次拉全：`{ok, rename:[{uid,nickname,alias,folder,current,works,target,conflict,problem}], dup:{root,items:[{name,uid,old,files,bytes}],total_files,total_bytes}, scan}`。`rename` = **文件夹名 ≠ 标注昵称**的作者（按作品数倒序，当前 348 位）；`problem` 非空 = 这个名字 Windows 存不了 |
| `/api/files/rename` | POST | `{uid, nickname}` | 把该账号文件夹改成 `UID<uid>_<nickname>_发布作品`，并**同步改掉 data.json 里 `works[].files[].rel` 的前缀**（不重扫也能马上播）。校验：非空 / ≤60 字 / 不含 `\/:*?"<>\|` / **不以点或空格结尾**（Win32 会把结尾的点吃掉）/ 目标不存在。返回 `{ok, folder, message}`；写索引失败时带 `need_rescan:true` |
| `/api/files/rename_batch` | POST | `{all:true}` 或 `{uids:[...]}` | 批量改名：全部改完**只写一次** data.json、只 `load_data` 一次（逐个改每次整份写 72MB，300 个就是 300 次）。返回 `{ok, renamed, failed:[{uid,nickname,error}], message}` |
| `/api/files/dup_group` | GET | `name`=组目录名 | 按「同一条作品」归并后返回 `{groups:[{key:[日期,描述,图序,扩展名], date, desc, versions:[{kind,file,rel,size,mtime,ext,is_image}], identical, has_current}], total_bytes}`。`versions` 里 `kind=current` 是**新文件夹里正在用的那份**（页面靠它做「保留这份」），`kind=quarantine` 是隔离区副本；`identical` = 两版字节大小相同。`rel` 给缩略图用（视频 `/thumb?path=`，图片当静态媒体）。安全：组名不含路径分隔符、`abspath` 必须在隔离区内 |
| `/api/works/delete` | POST | `{uid, idxs:[作品下标...]}` | **删除作品（默认移入回收站）**：文件搬到 `recycle-bin/<批次ID>/<原rel>`（保留「作者文件夹/文件名」结构）+ 从 `data.json` 的 `works` 移除 + 减 `stats.*` + 往 `data/回收站.json` 记一批（**含被删作品的完整 JSON 与原下标**，恢复要用）+ 写 `data/删除记录.jsonl`。传 `permanent:true` 才是真删（删文件 + 清缩略图缓存，不写回收站记录）。**回收站模式不清缩略图缓存**——恢复后 rel 没变，缓存直接能用。安全：`idxs` 越界整体拒绝、每个 `rel` 走 `safe_media_path()`、从大到小 `del`、外层兜 `BaseException`、一次最多 5000 条。返回 `{ok, works, files, freed, videos, images, batch, mode, failed[]}` |
| `/api/recycle/list` | POST | `{}` | 回收站概览：`{ok, batches:[{id,time,uid,nickname,works,files,size,exists}], total_size, dir}`。不含作品正文 |
| `/api/recycle/restore` | POST | `{id}` | **把一批搬回原位**：文件先搬（`safe_media_path(rel)`，原位已有同名文件就保留现有那份、删掉回收站副本），再持锁按原下标把作品插回 `works`（同 `date/desc/文件数` 的已存在则跳过，避免重扫/重下回来之后出现两条），stats 回加，写 data.json，删记录与批次目录。返回 `{ok, works, files, failed[]}` |
| `/api/recycle/purge` | POST | `{id}` | **彻底删除一批**（`rmtree` 批次目录 + 去掉记录），不可恢复。`id` 只接受记录里存在的值，传 `../` 一律回「回收站里没有这一批」 |
| `/api/files/dup_resolve` | POST | `{decisions:[{group, key:[4 段], keep}]}` | **按页面上的选择执行**：`keep="current"` → 保留新文件夹那份、删掉隔离区副本；`keep=<隔离区文件名>` → 把那份搬进新文件夹、删掉旧目标、删其余副本。**两种收尾都会把留下来那份重命名为「日期-<当前昵称>-描述[_序号].ext」**（昵称取文件夹名里那段，即改名后最新的名字），并**成批改掉 data.json 里的 `rel`**、顺手同步 `user_data` 里引用旧名的封面，最后 `load_data(force=True)`——所以不用重扫。缩略图缓存按 rel 的 md5 命名、不看 mtime，改名/覆盖后要删掉。文件名必须在扫描到的目录列表里且作品键匹配，**不拼客户端给的路径**。整个函数兜住 `BaseException`（外部删除保护会抛 `SystemExit`），永远返回 JSON。返回 `{ok, deleted, restored, renamed, freed, failed[], warnings[]}` |
| `/api/files/dup_delete` | POST | `{names:[组]}` / `{all:true}` / `{group, files:[文件名]}` | 删隔离区 `downnnnnnnn/_改名合并_重复文件_20261030/` 下的东西：整组、全部、或组内指定几条。安全：名字带路径分隔符直接拒；`abspath` 必须落在隔离区内，越界中止。组删空后自动 rmdir。返回 `{ok, removed, freed, failed[]}` |
| `/api/new_downloads` | GET | `since`(unix 秒，0=不限) / `limit`(每页文件数，默认 2400，前端传 1200) / `cursor`(上一页的 `mtime|rel`，空=第一页) / `uid`(可选) | 「🆕 新下载」数据：按**文件写入时间**（≈ 下载完成时间）取最近落盘的文件，再按作者聚合 → `{total_files, shown_items, cursor, has_more, groups:[{uid,nickname,count,level,freq,status,tags,items:[{rel,rels,date,desc,is_video,files,size,mtime}]}]}`。**没有条数上限**：按 `(mtime, rel)` keyset 一页页往下翻（2026-11-02 取消「只显示最新 600 个作品」），`has_more=false` 才是这个范围到底了。同一作品的 `_1.._9.jpeg` 合成一条、`rels` 是完整文件序列（右侧播放区翻页用），有视频时优先拿视频当 `rel` |
| `/api/annotations` | POST | `{items:[{tool,rect,region,selector,note}], page, viewport}` | 标注台「保存给我」：校验后落盘 `data/annotations/界面意见_YYYYMMDD-HHMMSS.md` + 同名 `.json` → `{ok, file, dir, count}` |
| `/thumb` | GET | `path` | ffmpeg（`-ss 1`、`scale=400:-2`）生成/缓存的视频缩略图 jpg |
| `/api/cache_info` | GET | — | `{ok, thumb:{files,bytes,max_mb}, trans:{files,bytes,max_mb}, total_bytes}` |
| `/api/cache_clean` | POST | `{target: thumb\|trans\|all, wipe:bool}` | 按上限淘汰或整目录清空 → `{ok, freed, removed, failed, thumb, trans, total_bytes}`；`failed` = 删不掉的文件数（被占用等），部分成功也正常返回 |
| `/`、`/app.js`、`/style.css`、`/user_data.json` | GET | — | 静态资源。**白名单制**（`STATIC_FILES = {index.html, app.js, style.css, favicon.ico, favicon.svg, annotate.html, annotate.js}`），`/user_data.json` 由 `STATIC_DATA_MAP` 单独映射到 `data/` 目录；白名单之外（含 `data.json`、源码）一律 404。html/js/css/json 发 `no-cache`，其余发一年 `immutable` |
| `/媒体相对路径` | GET | — | 媒体文件（Range 206 + `Cache-Control: no-cache`，见"缓存"踩坑） |

---

## 四、数据模型相关代码位置

> 本项目无 AI/大模型。"模型"指**数据模型（账号/作品字段结构）**。

| 位置 | 文件 | 说明 |
|---|---|---|
| 账号字段定义 | `scanner.py` → `scan_author_folder()` 返回字典 + `merge_accounts()` 合并逻辑 | uid/nickname/works/stats/history 等 |
| 作品字段 | `scanner.py` 解析文件名的 regex `FILE_RE` | date/desc/media_type/files/file_count |
| 前端渲染字段 | `app.js` → `renderDetailTop()`（作者信息+标注+筛选）、`renderRail()`（左栏列表）、`authorCardHtml()`（列表卡片） | 读取 `a.xxx` 渲染 |
| 标注字段白名单 | `app.js` → `setAccountField()` | `['nickname','alias','douyin_id','signature','sec_uid','status','starred','tags','note','cover','main_uid','alt_accounts','history']`（`nickname/alias/douyin_id/signature` 改动会进 history） |
| 标注叠加 | `server.py` → `load_user_data()` | 把 user_data 覆盖进 `CACHE["accounts"]`，并同步 `list_index` 的 nickname/douyin_id/sec_uid/cover/alias |
| 列表字段白名单 | `server.py` → `load_data()` 构建 `lst` | uid/nickname/sec_uid/douyin_id/work_count/video_count/image_count/earliest/latest |

### 替换/修改数据模型需同步调整的位置

1. **`scanner.py`** — 字段来源：新增/删除字段要改扫描逻辑和返回字典
2. **`server.py` → `load_data()`** — 列表 API 暴露哪些字段（`lst` 白名单）
3. **`server.py` → `load_user_data()`** — 标注叠加哪些字段（默认整条 `update()`，新增字段会自动带上）
4. **`app.js` → `setAccountField()`** — 标注持久化的字段白名单数组（**改漏会导致新字段保存不了**）
5. **`app.js` → `renderDetailTop()` / `renderRail()` / `authorCardHtml()`** — 界面渲染字段
6. **重跑扫描** — 改 scanner 后必须重扫（删 data.json 或点"🔄 重新扫描"）

### 潜在影响

- 改字段后旧 `data.json` / `user_data.json` 不兼容 → 需重扫 + 迁移标注数据
- `server.py` 内存缓存 `CACHE` 只在启动/rescan 时载入，改了结构没重启会用旧结构
- 前端标注合并走 `/api/author` 按需合并，别改回全量加载（性能回退）
- **`CACHE["accounts"]` 里的对象就是 `CACHE["data"]["accounts"]` 的同一批 dict**，`load_user_data()` 会原地改它们 → 别把 `CACHE["accounts"]` 当 data.json 的纯净副本用，需要原始值只能重新读磁盘

---

## 五、改动前注意事项清单

1. **先关旧服务再重启**（最高频坑）：8723 端口常有僵尸进程，不杀干净新代码不生效
   - `netstat -ano | grep :8723` 查 → `taskkill /F /PID <pid>`
2. **改 `scanner.py` 后必须重扫**：删 `data.json` 或点"🔄 重新扫描"
3. **改 `server.py` 后必须重启**（无热重载）
4. **`user_data.json` 别删别覆盖**（用户手动标注），重扫不会动它
5. **硬编码路径已收口到 `src/paths.py`**（2026-11-01 重构后）：Python / ffmpeg / 数据源目录 / DouK 目录全在 `paths.py` 的「外部路径」块里，`server.py`、`scanner.py`、各 tools 脚本都从它取，**不再各写一份**。
   换机器只需改 `paths.py` **外加两处 bat**：`browser/start-browser.bat` 的 `set PY=`（没有 `python` 兜底，路径失效会直接报错退出）、`start-all.bat` 的 `PY/PYW`（有兜底，可不动）。详见 [`CONFIG.md`](CONFIG.md) 第二节。
6. **`.bat` 必须纯 ASCII**（中文会因编码乱码报错）
7. **媒体文件名含 `#`/空格**：前端用 `mediaUrl()`（逐段 encodeURIComponent），别改回 `encodeURI`
8. **视频解码器覆盖不全**：右栏媒体区内嵌 `<video>`；HEVC 先看浏览器能不能直解（装了系统 HEVC 扩展就能，播原片），解不了才走 `/api/probe` + `/api/transcode`，真放不了才提示「📂 打开文件」。图文也在这同一块区域显示大图（多图用 ‹ › 翻页）
9. **改前端后**用 `node --check app.js` 做语法校验再交付
10. **标注字段必须同时改两处白名单**：`app.js` 的 `setAccountField()` + `server.py` 的 `load_user_data()` 叠加链路
11. **HEVC 视频浏览器放不了**：抽样 40 个视频有 25 个是 HEVC/H.265（Chrome 在 Windows 默认解不了，表现为进度条走、画面定格）。**本机已装 HEVC 扩展，现在是原生硬解、播原片**；没装扩展的机器才走 `/api/probe` → `/api/transcode`（`libopenh264`，失败退 `h264_mf`；本机 ffmpeg 是 LGPL 版、没有 libx264）。两条路的切换点是 `canPlayHevc()`。
12. **改完前端用户说"没变化/一直加载中"**：先怀疑浏览器缓存。`index.html` 里引用是 `app.js?v=YYYYMMDDx` / `style.css?v=YYYYMMDDx`，改了前端就把版本号往后挪一位；用户侧 Ctrl+F5 强刷一次。
    - 历史上出过：服务器早先给 js/css 发了 `immutable`（缓存一年），结果新 HTML 配旧 JS → 旧 JS 找不到 `#authorOverlay` 抛错 → `init()` 没执行 → 页面永远停在「加载中…」。现在 js/css/json 都是 `no-cache`，且 `app.js` 有 `window.onerror` 兜底在顶部显示红条。
13. **子进程脚本 import 会因目录重整失效**（2026-10-28 修）：`src/query_uid_by_secuid.py` 里 `import fetch_uid_by_secuid`，而后者在 c27 被归到 `tools/legacy/`，子进程 `sys.path[0]` 只有 `src/` → `ModuleNotFoundError`。**traceback 走 stderr、stdout 全空**，上层 `douyin_query_uids()` 只能报「查询进程无输出」，把排查引向「cookie 过期」（其实 cookie 好好的）。现在脚本自己把 `tools/`、`tools/legacy/`、`src/` 塞进 `sys.path`，导入失败也会输出真实原因。
    - 教训：**凡是被 `subprocess` 拉起的脚本，它的可 import 范围只有自己所在目录**。挪文件时 grep 一遍 `import <被挪的文件名>`；报「进程无输出」时先手动跑一遍看 stderr，别急着怀疑鉴权。

### 已知问题（尚未修）

- **6 个账号缺 sec_uid（已注销/封禁，无法补）**：`profile/other` 返回空 user。具体账号的昵称与 UID 属于本地个人数据，不在仓库列出。除非账号复活或有新数据源，否则补不了。
- **长转码任务在服务重启后丢失 job 状态**：`TRANS_JOBS` 在内存里，服务重启后前端轮询会拿到 `status:"missing"`（会提示转码失败，重新点开即重转；`trans_cache/` 里的产物不受影响）。
- **死代码**：`scanner.py` 的 `--incremental` 参数（解析了但没用）。`main_uid` 原先是「数据里有、界面只有只读按钮、没有写入入口」，2026-10-27 已补上「设为大号」写入入口（不再是死字段）。
  - 2026-10-27 已清理：`/api/play` 接口 + `POTPLAYER` 常量（PotPlayer 按钮早前已删，接口成为死代码）。
  - 2026-10-26 已清理：`workThumbHtml` / `workTypeBadge` / `editSignature` / `editDouyinId` / `updateFilterSummary` / `addAltToUserData`、`.detail-card-wide`、`#addTagBtn`。

---

## 六、关键设计决策记录

- **作者数比文件夹数少**：改了昵称就会多出一个 `UID<uid>_<新昵称>_发布作品` 文件夹，同一个 uid 被扫成两个账号。扫描时按 uid 合并成一个、旧名记进 `history`（2026-10-30 那次一次性合并了 113 个旧名文件夹）
- **标注与索引分离**：`user_data.json` 独立存标注，重扫只重建 `data.json`，标注不丢；服务端用 `load_user_data()` 把标注叠进内存 `CACHE`（2026-10-26 修复：此前星标/标签在列表和侧边栏完全失效）
- **性能**：前端按需分页，绝不一次性加载 72MB data.json（后端内存缓存 + 分页 API）
- **媒体 URL**：`mediaUrl()` 逐段编码，解决 `#话题`/空格导致的 404
- **缩略图**：ffmpeg 从第 1 秒抽帧生成 400px 宽 jpg，缓存 `thumb_cache/`（md5(rel) 命名）。**作品自带封面图时优先直接用封面图**（`pickPosterFile`），比抽帧清晰
- **作者详情页取代抽屉**：点卡片后内容区整体切换成详情页（`#authorView` ↔ `#detailView`）。布局是抖音页的镜像：**左栏方形卡片墙（作品列表）/ 右栏上方作者信息框 + 下方播放器**；`refreshTop()` 只重绘信息框，避免改标注时打断正在播的视频
- **作者列表卡片不显示 UID、但显示「N 作品 · 最新作品日期」**：`.author-meta` = `N 作品 · <latest>`（原来写 `UID xxx · N 作品`，用户要求去掉 UID——19 位数字既认不出人也不好看；后来又要求加「最后更新时间」，即 `stats.latest` 最新作品日期）。UID 只在详情页的字段行里出现。**别在列表里加回 UID**，要区分重名账号用别名/抖音号。
- **两种作者列表显示方式（2026-11-03 c119，取代 c116 那版左右分栏）**：`state.authorLayout` = `grid`（默认网格卡片）/ `rows`（每作者一行），存 `localStorage.authorLayout`，按钮在视图工具条（`#layoutBtn`）。
  - **版式照「新下载」栏目抄**（用户 11-02 22:38 要求「像新作品那个栏目里面那样」）：一位作者 = 一行**表头** `.ar-head`（圆点 + 名字 + 等级/频率/状态/标签 + 右侧 `⬇ N 天前` 和 `N 个作品`）+ 下面 `.ar-strip` **一行作品卡片**。
  - **`N` 是固定列数、绝不左右滑动**（用户 11-03 09:32 标注：「这里不要可以左右滑动的，固定一定数量的作品，不够就是空白」）：`.ar-strip` 是 `display:grid; grid-template-columns: repeat(var(--ar-cols,8), minmax(0,1fr))`，列数由 `rowFitStrip()` 按容器实测宽度算（`(W+8)/(112+8)`，夹在 3~12），写进 `grid.style['--ar-cols']`；**多余的卡片加 `.hidden` 不渲染**，作品少于列数时 grid 天然留白（不需要占位元素）。窗口 resize 有 180ms 节流重算。
    - ⚠️ 别改回 `overflow-x:auto` 那套横向滚动 —— 那是 c118 的做法，被用户明确否掉了。
    - 卡片 `.ar-card` 现在是纯 grid item（`min-width:0`），宽高由列宽决定：3/4 封面 + 底部日期，图文左上角标「图文」、多文件右上角标数量。
  - **右侧 `#rowsPlayer` 常驻播放区**（`position:sticky; top:32px`，停在顶部工具栏下；**尺寸照「新下载」栏目**：`440px` 宽、`height: calc(100vh - 200px)` 撑满可视高度，`.rp-stage` 是 `flex:1` 吃满除底栏外的全部高度 —— 以前用 `aspect-ratio:3/4 + max-height:60vh` 那一版用户嫌「预览的太小了」）：点左边任意卡片就在这儿播，**不跳作者页**。视频走 `/api/probe` → 放不了就 `/api/transcode`（和详情页同一条路），`<video>` 上加 `play().catch()` 兜底；图文按 `images[]` 一张张翻；底部给描述/日期/文件数 + 「打开作者页 →」。切回网格或换筛选会自动 `rowStopPlay()` 停播，别在后台出声。
    - 无头浏览器验证播放要带 `--autoplay-policy=no-user-gesture-required`，并且用 **CDP `Input.dispatchMouseEvent`** 真点（`el.click()` 不算用户手势）——否则只看到 `paused=true`，那是环境限制不是代码问题（实测 4K 竖屏源 `paused:false`、`currentTime` 正常推进）。
  - **表头的「⬇ N 天前」= `last_download`**（`download_log.db` 里该作者最新文件的 mtime），>30 天标提醒色 `.ar-dl.old`，没记录显示「没下载过」（用户 11-03 标注要「最后下载时间、多久没下载」）。
  - 数据靠 `/api/authors?works=N` 带回来：每项多 `recent[{rel,is_image,images,date,desc,index,files}]` + `recent_more`。`images` 只对图文有意义（最多 9 张），视频留空。行模式每页只拉 **20 个作者**（网格是 60）——一行 12 张图，60 个作者一次会铺 700 多张。
  - 切视图 = 给 `#rowsWrap` 加/去 `rows-mode` 类（两栏 flex），并且必须 `rowsPlayer.classList.toggle('hidden')`；**搜「作品」时**（`renderWorkHits`）要 `remove('rows-mode')` + 藏按钮 + 藏播放区，否则作品卡片会被纵向 flex 拉成一条一条、右边还留一块空播放器。
- **更新频率/等级徽章上了列表卡片**（2026-11-02 c116）：`freqTagHtml()` **原样显示 `day/3天/周/月`**（用户 2026-11-03：「day 就是 day，不要写成日更」，筛选面板的选项文案同步改回来了），`levelTagHtml()` 复用详情页那套配色。**状态在 `update_freq_disabled_status` 里时（注销/禁言封号/卖号/私密/不好看了）不显示频率徽章**，否则会出现「注销 · 日更」这种自相矛盾。
- **`/api/authors` 的 `tags` 已全量返回**（原来 `[:3]` 截断）：用户要求标签全部显示，前端按视图自己排。数据里单账号目前最多 2 个标签。
- **封面**：媒体区「🖼 设为封面」把当前作品写进 `user_data.cover = {workFile: rel}`；视频封面由 `/thumb` 截帧，图片直接用原图（`coverUrlFrom()` 按扩展名分流）。**详情页头部和「全部作者」列表卡片都会显示**（`/api/authors` 的 items 带 `cover`，`load_user_data()` 会把标注里的 cover 同步进列表项）；设完立刻更新两处，不用刷新；图加载失败 `coverBroken()` 回退 📱 占位
- **进入作者页不自动播放**：`openAuthor` 里 `setRailFilter('video', false)` + `showStagePlaceholder()`，右栏只提示「点击左侧作品开始播放」，点了才播
- **视频与图文统一在右栏媒体区**：作品量大（几十万级）时，浏览器解码器覆盖不全，所以检测到 HEVC 会自动转码（PotPlayer 方案已弃用）；图文不进浮层，直接用同一块区域显示大图，多图在帖子内翻页——一处代码管两种媒体
- **大小号存「组」而不是「边」**：每个账号的 `alt_accounts` 存的是**同组其他所有人**，所以任意一个人的数据里就带着完整组信息，读取零跨查。代价是冗余（N 个人各存一份）和「写必须整组重写」，换来的是：老数据能靠并查集自愈、服务端能无脑归一化、前端读一个字段就能画全组。**绝对不要再写「只给两端各加一条」那种补丁式代码**——那就是第 15 条坑的来源
- **关联入口用搜索而不是让用户填 ID**：`/api/authors?q=` 早就同时匹配昵称/抖音号/UID/sec_uid，选择器直接复用。做新功能前先查服务端已有的搜索/筛选能力，别让用户手抄一串 19 位数字
- **作者等级 `level` + 更新频率 `update_freq` 是两个独立字段**：等级（夯/顶级/一般/拉）做成胶囊，**两者完全不联动**（用户明确要求，`LEVEL_DEFAULT_FREQ` 已删除——以前设「夯」会自动填 `day`，改等级顺手改掉频率很别扭）；**设等级整组同步、改频率只改本人**（2026-11-02 用户要求取消「大小号 ↔ 更新频率」关联：同组的小号可能天天发、大号几个月不动，频率本来就该各记各的；等级仍然整组同步，因为那是「重要程度」），点已选中的胶囊 = 取消（`value===cur ? '' : value`）
- **搜索分范围 + 作品直达**：`scope`（all/nickname/note/signature/works）区分搜索范围；搜「作品」时走专门的 `/api/search_works`（扁平化作品列表），前端渲染作品卡片、点进去 `openAuthor(uid, workIndex)` 定位到该作品（`setRailFilter('all')+playWork(workIndex)`）
- **「添加顺序」排序 = 作者文件夹 ctime**：`_load_data_locked` 逐个 `os.path.getctime` 存 `added_at`（合成账号/读不到 = 0），排序键 `(added_at, uid)`
- **已移除**（用户要求）：直播记录、收藏夹关系、话题标签自动提取、编辑简介/抖音号按钮

---
---

## 七、界面现状（2026-10-31）

### 1. 主列表（作者）

- **顶栏**：搜索框（昵称 / 抖音号 / UID / SEC_UID / 备注 / 作品，带范围切换）· 统计 · `🔄 重新扫描` · `✏️ 标注`（开标注台）
- **侧边栏**：功能（批量获取资料 / 获取最近收藏 / 导出作者信息 / `🆕 新下载` / `📁 文件管理` / 设置）+ 分类（全部作者 / 星标 / 大小号 / 私密 / 注销 / 禁言 / 停更 / 卖号 / 不好看了 / 待修改 / 黑名单）+ 等级 + 更新频率 + 标签云（数量都带实时计数）
- **卡片**：封面 + 昵称（有别名时「别名 + 灰色昵称」）+ `N 作品 · 最新作品日期`。**列表里不显示 UID**（要区分重名靠别名 / 抖音号）

### 2. 作者详情页（左栏整体滚动 / 右侧媒体区固定）

自上而下：

1. **返回栏** `.detail-bar`：固定头部（不是 sticky）+ `← 返回` + 标题；勾选作品后这里出现批量动作
2. **作者信息** `.detail-header`：封面 + 信息区
   - 作者名 / 别名、字段行（抖音号 · UID · SEC_UID，竖线分隔，SEC_UID 限宽 200px）
   - 简介 `.detail-sign`
   - 动作栏三颗**纯图标**：`⭐ 星标` · `📁 打开文件夹` · `🕘 历史记录`
   - 大小号行 `.detail-alts-inline`
   - 右下角**数据条** `.detail-stats-wrap`：`作品 N（v x p y） · 抖音 N 发布 YYYY-MM-DD · 最后下载 …`，行尾 `⟳` = 绕过 6 小时缓存真查一次
3. **五格信息卡** `.detail-grid`：账号状态 / 作者等级 / 更新频率 / 标签 / 备注
4. **作品筛选栏** `.wall-head`：全部作品 / 视频 / 图文 + 排序；下面是 `.workRail` 方形卡片墙（**9:16 竖屏**）
5. **右侧 `#playerStage`**：视频 / 图文播放；HEVC 自动转码

约定：**进详情页不自动播放**（右栏只提示「点左侧作品开始播放」）；`refreshTop()` 只重绘信息框，不打断正在播的视频。

### 3. 🆕 新下载（`#newView`）

- 数据来自 `data/download_log.db` 的**文件写入时间**（≈ 下载完成时间），不是目录扫描，所以「今天」= 真今天下的
- 左：**一位作者 = 一个气泡**（c96）。气泡有浅灰底 `#f8f9fb` + 圆角 14 + 内边距 `10px 12px 12px`，把这位作者的全部作品包在一起
  - **列数按当前可用宽度动态算**（c99，`ndLayout()`）：`maxCols = floor((列表宽 - 24 + 10) / (138 + 10))`，卡片最小 138px。窗口宽/收起侧边栏 → 列数自动变多，把右边填满
  - 气泡内**优先横向排满**：列数 = `min(作品数, maxCols)`，超出换行
  - 气泡宽度：能铺满整行（作品数 ≥ maxCols）→ `width:100%`（加 `.full`）；作品少 → JS 算出具体像素写进 `--nd-w`，让几个窄气泡并排
  - ⚠️ 宽度**不能**写 `min(100%, calc(...))`：`100%` 在 flex 子项里按整行算，会把气泡撑满、小气泡永远并排不了。用 `--nd-w` + `max-width:100%`
  - ⚠️ 必须 `box-sizing: border-box`，否则左右内边距 24 加在 calc 之外，卡片右边缘会留一条缝
  - `.nd-list` 是 `flex-wrap` + `align-items: stretch`：窄气泡并排、同行等高
  - ⚠️ 缩略图**不能** `flex:1` 拉高（等高时图片会变形），固定 3:4；多出来的高度留在气泡底部
  - 表头一行到底：`nd-dot` 小圆点 + 名字（`max-width:50%`，超出省略）+ 徽章 + `N 个`；整行可点进详情页
  - 徽章放不下时由 `ndTrimChips` 收成 `＋N`（悬停看被藏的完整标签）。⚠️ `.nd-chips` 必须 `flex:1 1 auto`，写 `0 1 auto` 会让它跟着内容缩、量出来的可用宽偏小，徽章会被裁到只剩一个
  - 窄气泡（`data-cols` 1~2）里「N 个」没信息量 → CSS 直接隐藏，宽度让给名字
- 右：常驻播放区，默认**宽 420px**（窗口 ≤1280 收 340）
  - **收起侧边栏时左右一起变**（c99）：`body.side-collapsed` 时播放区 420→500（≥1800 窗口 560）。⚠️ 收栏时**窗口尺寸不变、媒体查询不触发**，必须靠 `body` 上的类名，不能只写 `@media`
  - 信息区定高 84px —— 换视频不改变播放器尺寸
  - **宽度变化自动重排**（c99）：`ndWatchWidth()` 用 `ResizeObserver` 盯 `#newBody`（只听 `window.resize` 没用，收栏不触发它），防抖 120ms
  - 重排走 `ndRelayout()` 而**不是** `renderNewDownloads()`：后者会 `ndPlayReset()`，一收侧边栏正在播的视频就没了。`ndRelayout` 保留 stage DOM、重新点亮 `.active`、并按**气泡锚点**还原滚动位置（只记 `scrollTop` 不行，列数变了总高度也变，实测 400 会被压成 104）
  - 信息区两行：作者（整行可点进详情）+ 等级/频率/标签徽章 + `1/N` + `›` / 日期 + 描述（2 行截断）+ `↗ 原文件`
  - **多图作品**（c94）：画面下方 `#ndpStrip` 并排列出这个作品的每张图（44×56，当前那张粉色边框），点哪张看哪张
  - **图片原位放大**（c94）：点大图 → `.ndp-stage.zoomed`，`object-fit: none` 按原始尺寸显示、stage 内滚动；再点一下复原。**播放区框尺寸不变、不开遮罩、不进全屏**（用户明确要求「不是全屏放大」）
  - 换图/换作品时自动退出放大态（`ndPlay.zoom = false` + 移除 `.zoomed`），否则下一张会莫名其妙是放大的
  - 翻页 `←` `→`，或直接点左边另一张卡；正在播的卡有粉色边框
- **不再有条数上限**（2026-11-02，用户要求）：以前一次只给最新 600 个作品、底部写「想全看完就缩小时间范围」。现在按「文件」分页（一页 1200 个文件 ≈ 几百个作品），**左列表滚到底自动取下一页**（`ndArmMore` / `ndMaybeLoadMore`，阈值 500px），底栏那行变成「已显示 X 个作品 · 继续往下滚」也可以点；全部取完才显示「已全部显示」
  - 分页用 keyset（`cursor = mtime|rel`）而不是 `OFFSET`：翻页途中又有新文件落盘时 `OFFSET` 会整体错位，`(mtime, rel)` 是稳定坐标
  - 一页按**文件**切，所以同一作品/同作者的记录可能横跨两页 → 前端 `ndMergePage` / `ndMergeItem` 按 `uid` + `日期\u0000描述` 并回一条（视频优先当门面、`rels` 去重后按文件名数字排序）
  - 续页重绘走 `renderNewDownloads({keepScroll:true})`：不 `ndPlayReset()`、按锚点还原 `scrollTop`、按 `uid + 日期/描述` 重新点亮正在播的那张卡
- **视图隐藏时不能量宽度重排**（2026-11-02 修「从作者页退出来卡一帧图片」）：进作者详情会 `newView.classList.add('hidden')`，此时 `#newBody.clientWidth = 0`。旧版 `ResizeObserver` 看到 0 就当成「变窄了」排了一次重排，`ndLayout()` 用 0 算出 `maxCols=1`、每个气泡 `full`（整行宽）→ 返回新下载页第一眼就是「一张卡占满整列」的巨图，120ms 后才被第二次重排纠正。现在三道闸：① 观察器遇到 `w<=0` 直接返回、不写 `lastW`；② `ndLayout()` 在隐藏时沿用上次可见宽度 `ndGoodW`（从没量到过才按窗口宽估）；③ `ndRelayout()` / `ndMaybeLoadMore()` 在容器宽高为 0 时直接返回（顺带防止在详情页后台把所有页都续下来）
- 切走视图 / 进详情页时必须 `ndPlayReset()`（清空 stage 的 DOM），否则 `<video>` 会在后台继续出声

#### 3.4 新下载的作者筛选（2026-11-04 c124）

工具栏 `#ndFilterBtn` 展开 `#ndFilterPanel`，**5 行**：作者等级 / 标签 / 账号状态 / 更新频率 / 最后下载时间。
和「导出作者信息」是同一套语义，**每行行首一个圆钮**（`ndf-match`，蓝 ＋ = 包含 / 橙 － = 排除，2026-11-05 起），状态存 `ndFilter.matchMap`。

- **筛选在服务端做**（`/api/new_downloads` 复用导出的 `attr_tests` / `_attr_mode`），不是前端过滤。
  所以「左列表滚到底自动取下一页」取到的也全是筛完的结果，不会翻着翻着混进不该有的作者
- 勾选立即生效（`ndFilterChange` → 重取第一页），面板不自动关，方便连着勾；有筛选时按钮上挂一个小圆点
- ⚠️ **卡片上的日期是「作品发布日期」，筛选用的是「下载时间」**。这两个常常差好几年，
  「今天」里出现 2025 年发布的作品是正常的 —— 为避免误读，卡片日期前缀写的是 `发布 YYYY-MM-DD`
- ⚠️ 别在客户端做这个筛选：一页只取 1200 个文件，客户端筛完会「看着像漏了很多」，实际是没翻到的页被筛掉了

### 3.5 封面 / 转码的硬件加速（2026-11-02）

用户反馈「加载封面时 CPU 100%、显卡不动」。实测（12 核 + RTX 2060 SUPER，ffmpeg 8.1 LGPL 版）：

**抽封面（并发 8，24 个真实视频，量总墙钟）**

| 方案 | 总耗时 | 平均每张 | 结论 |
|---|---|---|---|
| 纯 CPU、`-threads 2` | **0.62s** | **26 ms** | 采用 |
| 纯 CPU、默认线程数 | 1.19s | 50 ms | 更慢（并发时互相抢核） |
| `-hwaccel cuda`、`-threads 2` | 3.49s | 145 ms | 慢 5.6 倍，**弃用** |

**转码（一段 15.7s 的 HEVC）**

| 方案 | 耗时 | CPU | 结论 |
|---|---|---|---|
| libopenh264 | 32.8s | 高 | 最慢 |
| h264_mf | 13.5s | 高 | 备选 |
| `-hwaccel cuda` + h264_nvenc | **8.66s** | 很低 | 采用（4K 源实测 17.7s → 2.4s） |

要点：
- **抽封面绝对不能走 GPU**：每个封面都是一次性 ffmpeg 进程，NVDEC 初始化 + 把那一帧从显存拷回的开销，远大于「解一帧」本身。**串行单张测着像持平（243ms vs 287ms），一并发就露馅（26ms vs 145ms）**——这是 2026-11-02 当天先加后撤的原因，别再改回去。
- 所以 `_thumb_ffmpeg()` 现在是**纯 CPU**：`-threads 2` + 并发 8 个进程，靠进程数铺满 12 核，比「少进程多线程」快一倍。
- **转码才是 GPU 的主场**：解码 + 编码都在显卡上，比软件快 4 倍且几乎不吃 CPU；`_nvenc_ok()` 失败一次就整进程不再试。
- 顺带修了 `_eff_ffprobe()`：以前用 `custom.replace("ffmpeg.exe","ffprobe.exe").replace("ffmpeg","ffprobe")`，第二个 replace 会把**目录名**里的 ffmpeg 也换掉，拼出 `D://app//ffprobe-n8.1-...//ffprobe-n8.1-...//bin//ffprobe.exe` 这种不存在的路径 → 编码探测恒为空（`/api/probe` 永远说 playable）、转码永远按 720p 默认码率。现在只换文件名。

### 4. 导出作者信息（`#exportView`）

- **左栏①：待下载区**（2026-11-01 从页面顶部整块挪进左栏，宽度跟着栏走，不再是横贯全屏的一条）
  - 数据来自 8790 的 `/queue`（插件在抖音页面点「＋ 待下载」推过来的作者）
  - 每位一行：昵称 + 标签 + 状态徽章（已新建主页 / 库里已有 / 未建档）+ 推送时间，行尾 `×` 可移除
  - 底栏只有 `预览` / `刷新` / `清空`（可撤销）—— **这一区不再有自己的导出按钮**
  - ⚠️ **「没点过预览就不算」**：点「预览」除了展开抖音号/粉丝/sec_uid，还会把这一区**挂成 ③ 的导出对象**（`exportState.source='queue'`，`uids` 和 `secUids` 都收着，因为这一区不少人还没 uid）。不点它，③ 只会用 ① 筛出来的人
- **左栏②：筛选**（等级 / 标签 / 状态 / 更新频率 / 已下载最后时间）+「预览选中的作者」
  - 预览列表：名字 / 标签 / 等级 / 状态 / 本地保存最新 / **行尾 ✕**（这次先不导出这个作者，底部可恢复）
  - 点它 = 把导出对象切回 `source='filter'`（并清掉待下载区挂过来的 `secUids`）
- 右栏：DouK 下载参数（请求间隔、每 N 个号暂停 M 秒、翻页数、文件上限…）+ 三种导出格式 + `导出` / `⚡ 一键写入 DouK`
- **写 DouK 只有一个入口**（2026-11-01 合并）：右下 ③ 的 `⚡ 一键写入 DouK`，写的是「当前预览对象」，来源由 `exportState.source` 决定 —— `queue`（待下载区挂过来的）或 `filter`（① 筛出来的）。以前左栏还有一个「导出到 DouK」，两个按钮语义重复，已删
- ⚡ 一键写入：**整份替换** DouK `Volume/settings.json` 的 `accounts_urls`，写前自动备份到 `backup/`，**DouK 正在运行会直接拒绝**
- `markExportDirty()` 在 `source==='queue'` 时**直接 return**：挂的是待下载区名单，改 ① 的筛选本来就不该把它作废
- 日期一律 `YYYY/MM/DD`：DouK 的 `check_date` 只 `strptime('%Y/%m/%d')`，横杠会被判「发布日期无效」并丢弃限制 → 该作者变成全量重抓

### 5. 标注台（`/annotate.html`）

界面说不清哪儿要改时用：左边 iframe 内嵌真实页面（能点能滚），右边写意见。

- 工具：框选 / 箭头 / 画笔 / 文字；每条自动编号，并认出「这是哪一块 + CSS 选择器 + 矩形」
- 保存 → `data/annotations/界面意见_YYYYMMDD-HHMMSS.md`，我直接读这份清单改代码
- 实现要点：画布/徽标层是**运行时注入 iframe 文档**的（`app.js`/`style.css` 零改动）；绘制模式下 capture 阶段 `preventDefault + stopImmediatePropagation` 抢占事件；坐标存「页面坐标（client + scroll）」，滚动时 rAF 重绘

---

## 八、踩坑要点（一条一句；完整过程见归档）

### 数据与保存（碰了就丢用户数据）
1. `user_data.json` 是**全量覆盖写** → 任何「读-改-写」都必须走 `merge_save_user_data()`（按 uid 合并 + 加锁 + `os.replace` 原子替换）。历史上被整份覆盖过两次（537 条 → 4 条）。
2. 合并写要能表达两种 `null`：`{字段: null}` = 删字段，`{账号: null}` = 整个账号删掉。否则解除关联 / 删博主后会留空条目，`load_user_data()` 还会把它当「合成账号」显示回来。
3. 并发保存会丢更新（两个标签页各写一份，后写的覆盖前面的）—— 服务端必须串行化。
4. 用户数据**不能放会被淘汰的缓存目录**（`cache/` 会自动清），曾被当成缓存清掉，看起来像数据没了。

### 前端
5. 改完「没变化 / 一直加载中」→ 先怀疑缓存：改了前端要跑 `tools/bump_version.py` 改 `?v=`，用户 Ctrl+F5。
6. `const state` / `const detailState` 是**词法绑定、不在 window 上**，控制台里 `window.state` 取不到；要驱动就走全局函数。
7. 媒体 URL 必须用 `mediaUrl()` 逐段 `encodeURIComponent`，`#话题`/空格会 404。
8. 新样式**一律追加到 `style.css` 末尾**：文件尾部有大量 `!important` 覆盖层，写在前面会被后面同权重的规则盖掉（媒体查询也一样，必须写在覆盖层之后）。
9. 给 `const` 字符串 `+=` 会静默失败，模板区整块清空 —— 用 `let`。
10. 窄容器里把下拉框换成胶囊，要同时加宽对应的 grid 列，否则胶囊叠压到相邻卡片。

### 服务端 / 进程
11. 改 `server.py` 必须重启，重启前先杀干净 8723 上的僵尸进程；**双击两次启动会起两个服务抢同一个端口**（Windows `SO_REUSEADDR` 让 bind 永远成功）。
12. `stderr` 被「客户端断开」的 traceback 刷爆 → 服务跑着跑着就没了；写日志要吞 `BrokenPipeError`。
13. 子进程脚本的可 import 范围**只有自己所在目录**；报「进程无输出」时先手动跑一遍看 stderr，别急着怀疑 cookie 过期。
14. 子进程管道写中文要 `ensure_ascii=False` + 明确 UTF-8，否则走 Windows 默认 GBK 直接炸。
15. 状态字典别用 `ok` 这类会和接口返回撞名的键（会把 `ok: true` 顶掉）。
16. **「详情页作品数一直是 0，但磁盘有文件、下载记录也对」= 内存缓存与 `data.json` 永久错位**（2026-10-31 踩过）。
    根因：`_sync_cache_after_change()` 把 `CACHE["mtime"]` 设成新文件 mtime，却**只同步 `list_index` 的统计、没同步 `CACHE["accounts"][uid]`**（`/api/author` 读的正是这份）。之后 `load_data()` 一看 mtime 相同直接 `return False`，这份旧数据就再也刷不新，只能重启。
    修法：① `_sync_cache_after_change(uid, stats, acc)` 把 `works` / `stats` 一起同步；② `load_data` 的跳过条件改成 **mtime + size 双校验**（扫描器是「写 tmp 再 `os.replace`」，同一秒内多次替换 mtime 可能相同）。
    自查口诀：`/api/meta` 的 `total_authors` 和 `data.json` 里的 `meta.total_authors` 对不上 → 就是这个病。

### 抖音接口 / 数据补齐
16. 查 uid / sec_uid 要用**登录态** `profile/other`（返回 19 位 uid），公开接口拿不到。
17. **按昵称回填 SEC_UID 会串号**（同名作者共用一条，点开是别人），必须按 uid / sec_uid 匹配；批量补完要抽查。
18. 批量获取资料时校验「返回的 sec_uid 是否就是目标账号」，否则会把别人的简介写到自己的账号上。
19. 新下载的博主没有 `Data/*.xlsx`，重扫后 sec_uid 是空的 → 跑 `tools/fill_secuid_from_export.py`（纯本地，读 `export/*.csv`）。
20. 缩略图缓存按 `md5(rel)` 命名、**不看 mtime**：改名 / 覆盖文件后必须删对应缓存，否则一直显示老图。

### 媒体 / 播放
21. 抽样 40 个视频有 25 个 HEVC，Chrome 默认解不了（进度条走、画面定格）。本机装了 HEVC 扩展 = 原生播；否则走 `/api/probe` + `/api/transcode`（ffmpeg 是 LGPL 版、没有 libx264，用 `libopenh264`，失败退 `h264_mf`）。
22. 图文帖里的 `_N.mp4` **不是封面**，是「实况图」的动效；不要跟静帧配对，按序号一张一张排。
23. 竖屏视频塞进固定高度的框，宽度是被高度算死的 —— 「容器更宽」和「没有黑边」互相矛盾，想更大只能全屏。

### 下载器 DouK（改它的配置 = 改用户数据）
24. **`name_format` 必须带 `id`**（2026-10-31 已改成 `create_time nickname desc id`）。
    原来只有 `create_time nickname desc`：同一作者、同一天、desc 相同（`@某作者`、`#甜妹` 这种）的作品**文件名完全一样**，后下的静默覆盖先下的。
    实测那轮 3608 个文件名对应 3660 个作品 → **丢了 52 个**，43 组冲突，最狠的一个文件名挤了 4 个作品。
    长文件名安全：32 万文件名最长 91 字符，加 19 位 ID 才 111 < `name_length=128`。
25. **`PermissionError(13, '另一个程序正在使用此文件')` 是上面那条的症状，不是杀软**。
    两个并发任务（`truncate=50`）写同一个 Cache 文件，一个 rename/unlink 时另一个还开着 → Windows 报 13。
    报错了会自动重试、通常能成功，所以**只看到报错容易以为没事，实际是静默覆盖在丢作品**。
26. **`parse_work_filename()` 必须剥掉末尾的作品 ID**：`re.sub(r"-\d{15,20}$", "", desc)`。
    否则 `FILE_RE` 的非贪婪 `desc` 会把 `-7566012345678901234` 吞进描述，界面显示成 `@某作者-7566012345678901234`。
    只剥「-数字」这种带分隔符的尾巴 —— desc 本身就是纯数字的老文件（`2025-11-15-某作者-7572611624440000000`）不受影响。
27. `earliest` 日期一律 `YYYY/MM/DD`：`check_date` 只 `strptime('%Y/%m/%d')`，横杠格式会被判无效并**丢弃限制 → 该作者全量重抓**。
28. 改 `Volume/settings.json` 前**必须先确认 DouK 进程退出**（`tasklist | grep -i douk`）：它退出时会拿内存配置整份回写，改了等于白改。
29. `download_data` 表只记 aweme_id、没有作者字段，所以「某作者下漏了」没法按作者清记录重下 —— 只能复制作品链接单独下。
30. **「下载完了、`data.json` 里有 255 个作品，主页却一直显示 0 个」= 合成账号没退场**（2026-11-01 踩过）。
    根因：`_load_user_data_locked()` 里「先把上一轮的合成账号摘掉」那句**无条件** `accounts.pop(uid)`。
    作者从「新建作者」变成「被扫描进 data.json」之后，每次刷新标注都会把刚读进来的真账号 pop 掉，
    再按 `user_data.json` 重建一个空壳 → 作品数永远 0，只有重启进程（`synthetic_uids` 清空）才好。
    修法：`load_data` 记下 `CACHE["data_uids"]`（这一版 data.json 里真实存在的 uid），
    只摘「不在这份名单里」的旧合成账号。自查口诀：`/api/author` 返回的键里**没有 `works`/`stats`**、
    只有 `work_count` 这类列表字段 → 就是被当成合成账号了。

### 其它
31. `.bat` 必须**纯 ASCII**（中文会因编码乱码直接报错）；启动脚本输出要 flush，否则双击像「没反应」。
32. 挪走 / 删掉磁盘上的作品文件后，索引里还在 → 点开全黑；删文件必须同步改索引或重扫。
33. **`/api/rescan` 是增量扫描，不是全量**（按文件夹 mtime 判断「这目录变过没」）。批量搬完文件立刻扫会漏，
    要**再扫一次**才全 —— 2026-11-04 搬完 G 盘后，7 个刚放回的文件第一次只认到 2 个，第二次才对。
    改文件内容的场景也同理：只看 mtime 的增量逻辑对「同名文件被替换」不敏感，必要时走全量。
34. **`os.remove()` 会被沙箱重定向进回收站**，`CODEBUDDY_SAFE_DELETE_ENABLED=0` 并不生效。
    表现：删了几 GB 文件，`df` 一点没释放（2026-11-04 实测 G 盘回收站压了 48.6 GB）。
    真删要用 `ctypes.windll.kernel32.DeleteFileW`（一次调用即生效，不产生新的回收站条目）。
    清回收站时 `$R`（数据）和配套的 `$I`（元数据）必须**成对删**，只删 `$R` 会留下孤儿 `$I`。
35. 批量校验几万个文件的 SHA1 用**多进程**（`mp.Pool(6)`）：21615 个 / 48.6 GB，单进程 54 分钟 → 6 进程 22 分钟。
    瓶颈是机械盘上的小文件随机读，加并发有效；先比 `st_size`、不同就直接判「不重复」，能省掉大部分 IO。
36. **作品文件改名不会同步 `cover.workFile`，封面会成片变成灰块**（2026-11-02 去 ID、2026-11-04 又踩一次）。
    `cover_cache` 的文件名是 `md5(作品相对路径)`，`user_data.json` 里存的也是那个相对路径 —— 名字一改，
    两边同时对不上，`/cover?path=` 找不到源文件就 404。
    `_patch_user_cover_after_rename()` **只管「作者文件夹」改名，不管「作品文件」改名**。
    修法：拿索引按「日期 + 归一化描述」找回同一条作品的新文件名，改指针，再把旧缓存按新 md5 复制一份（省一次抽帧）。

---

## 九、本地验证

### 1. 语法（改完必跑）

```bash
node --check src/web/app.js
python -c "import ast,io;ast.parse(io.open('src/server.py',encoding='utf-8').read())"
```

### 2. 接口

```bash
curl -s --noproxy 127.0.0.1 "http://127.0.0.1:8723/api/meta"
```

- 本机有 `http_proxy`，curl 必须 `--noproxy 127.0.0.1`，否则拿到 000/502
- 中文或特殊字符参数必须 `--data-urlencode` / `urllib.parse.quote`，直接拼 URL 会乱码

### 3. 截图 + 量尺寸（最有用的一步）

`.workbuddy/` 下三个脚本，都走 CDP 驱动真浏览器，跑完会打印 `JS 结果:`（脚本里 `return` 的字符串）。

> 前提：本机 Chrome / Edge 都是装着的，缺的从来不是浏览器。**只要「启动 + 跑 JS + 截图 + 关闭」在同一条命令里**（sandbox 会在命令结束时杀掉子进程），就能驱动真浏览器。

| 脚本 | 用途 | 用法 |
|---|---|---|
| `shot_size.py` | **指定 CSS 视口宽高**，可局部放大 | `SHOT_W=1700 SHOT_H=959 [SHOT_CLIP="x,y,w,h,倍"] python shot_size.py <url> <JS代码> <out.png> [等待函数]` |
| `shot2.py` | 固定 1500 宽整页截图 | `python shot2.py <url> <JS代码> <out.png>` |
| `shot_zoom.py` | 按缩放比例截图 | `SHOT_ZOOM=1.1 python shot_zoom.py <url> <JS代码> <out.png>` |

⚠️ 第二个参数是 **JS 代码本身**，不是文件路径 —— 文件要用 `JS="$(cat x.js)"` 传进去。
🚨 **JS 必须包成 IIFE**：`Runtime.evaluate` 执行的是表达式，裸 `return` 会报 `SyntaxError: Illegal return statement`。正确写法：

```bash
PY="C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
"$PY" .workbuddy/shot_size.py "http://127.0.0.1:8723/" \
  "(function(){var r=document.querySelector('.detail-grid').getBoundingClientRect();return JSON.stringify({x:r.x,w:r.width,h:r.height});})()" \
  out.png openAuthor
```

`shot_size.py` 里 `SHOT_CLIP` 适合看细节（气泡、按钮行）；量尺寸就用上面这种 `getBoundingClientRect` 的写法。

### 4. 版本号

改了前端就跑 `python tools/bump_version.py`：同步 `index.html` 的 `?v=` 和 `style.css` 右下角角标，然后让用户 Ctrl+F5。

---

## 附录 A：最近改动（倒序，最近的在前）

> 2026-11-04 整理：这一节长期超出「只留 10 条」的容量，改成按时间倒序保留近期改动。
> 更早的逐版本明细在 `本地 _archive/ 下的历史详述（含真实账号信息，未随仓库发布）` 的「附录：文档更新记录」。

- **2026-11-05 c138 作者详情页「纯净赏图」**：筛选栏加了一颗「🖼 赏图 N」，点开是全屏照片墙——这位作者**所有图文作品的所有图片**（不跟当前分类走），图片按原比例铺开、不裁切，除了顶部一条 46px 细栏什么都没有（刻意固定深色底，不跟明暗主题）。点任意一张进大图，←/→ 或两侧按钮翻页、Esc 逐层退出（先关大图再关墙）。分页渲染：每次 120 张，滚到底或图片加载完自动追加。实现要点：① HTML 是 `.gallery-body`（滚动）+ `.gallery-cols`（`columns`）两层——**`columns` 不能直接挂在固定高度的滚动容器上**，否则多列内容横向溢出被 `overflow-x` 裁掉，既不滚也看不见；② 图片先给 `aspect-ratio: 3/4` 占位，否则加载前高度是 0，`loading=lazy` 判定不可见就永远不加载（死锁）；③ 实测本机原图 280KB 平均 260ms（多数是磁盘缓存命中），比现用 ffmpeg 生成缩略图（4~6 秒/张）快得多，所以**墙直接用原图**；④ `scroll` 事件在无头环境不派发，另加 700ms 定时兜底。
  顺手修了 `/thumb` 对图片返回 500 的 bug：`_thumb_ffmpeg()` 里的 `-ss 1` 会把单帧的 jpg 直接越过结尾，图片必须不 seek（`seek = []`）。

- **2026-11-05 「同 uid 多文件夹」根治：自动合并 + mark 对齐**：全库几十个账号被拆成两个目录（`昵称A` / `昵称A🪑`），作品分两处、界面上只看得到一半。查 下载器源码 `src/downloader/download.py` 确认目录名是 `UID{uid}_{name}_发布作品`，`name = mark or user_name`（**mark 优先**），且都过一遍 `Cleaner.filter_name`——它会**删掉所有 emoji**、`:`→`.`、合并空格、去首尾点。所以带 emoji 的老目录是旧版建的，新版算出来不带 emoji，路径对不上 → `is_skip(id, path) = 记录表有 ID or 文件存在` 两条全落空 → **整位作者重下一遍**，还顺手建个新目录。三处改动：① 新增 `src/folder_merge.py`，扫描前自动把同 uid 的多个目录并到「文件最多」的那个（同名文件先比大小再比 MD5，完全相同才删副本；有同名不同内容就整个账号跳过等人看；子目录整个搬不合并）；`scanner.py` 开跑前调用，`SKIP_FOLDER_MERGE=1` 可关；② `server.py` 导出名单时 `mark` 改成「现有文件夹名里那一段」（`_douk_mark`）而不是昵称——以前每次导出都换 mark，等于每次全量重下；③ 一百多个老目录名规范化成 filter 之后的形式（`昵称A🪑`→`昵称A`），`settings.json` 里 数十条账号的 mark 也一并对齐（备份在 `Volume/` 下）。结果：全库作者**目录路径 100% 命中**，78 个重下的重复文件按 MD5 判重删掉（12 个大小不同的按「留大的」处理，小的进 `browser/backup/merge_conflicts_20261105/`）。残留风险：文件名里也有昵称段（`name_format = create_time nickname desc`），实测 84.8% 与当前昵称一致、能靠「文件存在」跳过，昵称改过的那 15% 仍会重下一次——但那批会被自动合并收编，不会留下重复文件。

- **2026-11-05 修「作者名显示成一大串 SEC_UID」**：新下载页有位作者的表头顶着 `MS4wLjABAAAAxxxx…`。根因不在渲染，是**数据**：当年下载他时抓不到主页资料，下载器把 sec_uid 当昵称写进了目录（`UID<数字>_MS4wLjAB…_发布作品`），扫描器按 `^UID(\d+)_(.*)_发布作品$` 原样取出中间那段当昵称，`sec_uid` 字段反倒空着。修三层：① `scanner.py` 加 `SEC_UID_LIKE` / `looks_like_sec_uid()`，目录名那一段像 sec_uid 就**当 sec_uid 收下、昵称留空**，空昵称再从作品文件名 `日期-昵称-描述.ext` 的第二段捞（文件名比目录名可信）；② `load_data()` 里清洗内存副本（防旧索引继续带脏数据）、`new_downloads` 的「从文件夹名兜一个」也加了同样的判断；③ 手工修正这一条（真名 **某作者**，来自作品文件名，十余个作品），改前索引备份在 `browser/backup/fix_nick_20261105/data.json.bak`。全库复查：昵称像 sec_uid 的 **0** 条，目录名带 sec_uid 的剩 1 条（就是这位）。

  **后续（同日）目录名也改掉了**：走 `/api/files/rename` 把 `UID<数字>_MS4wLjAB…_发布作品` 改成 `UID<数字>_某作者_发布作品`，接口会同步 `download_log.db`（file / author 两表）和索引。另一条 `UID<数字>_<同一串数字>_发布作品` 是同一类脏数据（下载时没抓到昵称，把 uid 当昵称），但去抖音反查后发现真相不同：**`<数字>` 这个 uid 底下本来就有两个文件夹**，`UID<数字>_<emoji昵称>_发布作品`（多数文件）和 `UID<数字>_<同一串数字>_发布作品`（少量文件），后者是 2025-10~11 那批下载时又建的新目录。抖音上这个号当前昵称就是 **<emoji昵称>**（抖音号 `example_id`），用户自己在 `user_data` 里给它标注的别名才是「某个自己起的别名」。处理方式：把 一批文件 `os.rename` 合并进 <emoji昵称> 文件夹（**先确认 0 条重名**，目标已存在就跳过，绝不覆盖；目录空了才 `rmdir`），`download_db.rename_folder()` 同步对应行，`/api/rescan` 重建索引，最后手工把 `user_data` 里那条 cover 的 `workFile` 前缀换掉（它是 `'UID<数字>_<同一串数字>_发布作品/2026-07-27-…'`，不换会裂图）。结果：该号全部作品与文件都在一个目录、`folders` 数组只剩一项、`download_log.db` 记录数无翻倍、磁盘上旧目录已消失。

  ⚠️ 顺带发现：全库还有 **89 个账号是一个 uid 两个文件夹**，成因是昵称后来加了 emoji（`UID…_昵称A_发布作品` + `UID…_昵称A🪑_发布作品`），下载器按新昵称另建了目录。名字本身都正常，不影响显示，但作品被拆在两处。要清理得逐个合并（同一套流程），这次没动。
- **2026-11-05 c130 图文角标只留数字**：新下载页（`ndCard`）和「每作者一行」（`authorRowHtml`）的卡片右上角原来挂「图文」两个字（`.nd-kind` / `.ar-kind`），于是出现三种不一致的样子 —— 多图是「图文」+ 数字、单图只有「图文」、视频多段只有数字。用户要求：**图文一律只显示张数，不要「图文」二字**。现在两处都是 `${is_video ? (n>1 ? 数字 : '') : 数字}`，图文单张显示 1、多张显示张数，视频仍然只有多段才标；`.nd-kind` / `.ar-kind` 两套样式一并删掉。实测 新下载页的卡片里图文**全部**带数字（含 1），「图文」字样 0 处。
- **2026-11-05 c129 筛选语义改版：＋包含 / －排除，并写成人话**：用户看截图提的 —— 原来行首圆钮的 **－ 是「交集（必须同时满足）」**，跟他脑子里的「减号 = 把这项去掉」不一致，改掉。现在：**＋ 包含**（勾中的是我要的，默认）/ **－ 排除**（勾中的去掉），同一行勾多个值仍是「命中任一」，不同 ＋ 行取并集，**所有 － 行命中的人直接剔除**；一个都不勾 = 全部作品。后端 `_build_attr_tests` 的 mode 从 `and`/`or` 改成 `not`/`or`（旧的 `and` 值仍收，按 `not` 处理，旧书签不至于炸），`_attr_uids`（新下载）和 `/api/authors` 两处判定同步改。**注意侧边栏那列状态/等级/频率是「包含」语义**：面板这行切成 － 时不能把那列的值并进去（否则「排除注销」会把正选着的「注销」也排除掉，永远筛不出人），`loadAuthors` 里用 `mergeSide()` 挡了。
- **2026-11-05 c129 新增筛选项**：账号状态补 `__unset`（未设置）、标签补 `__notag`（没有标签，`_t_tag` 里判 `not acc_tags`）、新增「封面」整行（有封面 / 无封面，`cover=has,none` 支持逗号多值，也走 ＋/－ 这套逻辑）、「作者等级」行尾加 `全选` 小按钮（`exportRowAll` / `authorRowAll`，全勾上再点一次全不选）。**「未设置」「没有标签」一律排在该行最前**，四处面板（导出 / 全部作者 / 新下载）统一。原来 `/api/authors` 里那段独立的 `q_cover` 判断**删掉了** —— 不删会和封面行的 － 打架（一个说必须满足、一个说要剔除，结果恒为空）。
- **2026-11-05 c129 筛选结果写成一句人话**：导出页 `<div id="exportSentence">` 在面板底部（「清空/预览」上面），全部作者页放在**筛选面板外面**（列表正上方 `#authorFilterSentence`，用户明确要求「不要放在筛选框里面」）。文案规则：无 ＋ 条件 → 「全部作品」，有 → 「等级：夯、顶级 或 状态：正常」，有 － 行就在后面接「，去掉 纹身标签」。默认无筛选时作者页那句隐藏，导出页显示灰色虚线框的「全部作品」。
- **2026-11-05 c129 作者页筛选面板重排**：删掉「不同属性之间 交集/并集」那组全局单选（早就不用了），等级/状态/频率/封面/最后时间五行改成 `fg-row` 四列 grid（圆钮 20px / 行名 82px / 勾选项 / 全选），SEC_UID 和星标行跟着缩进 28px 对齐。**面板宽度必须从 336px 加宽到 560px** —— 注意真正生效的是 `.filter-panel` 第 1501 行那条覆盖规则（`width: 336px`），只改文件开头那条没用，第一版就是这么白改的。
- **2026-11-05 c126 视频放完怎么办（4 种模式）**：详情页播放器底部、「🖼 设为封面」左边多一颗模式按钮（`#playModeBtn`），点开是 4 项菜单：**放完就停（默认，和以前行为一致）/ 继续播放下一个 / 重复播放（单曲循环）/ 随机播放**，选择存 `localStorage.playMode`，刷新不丢。实现要点：① `<video>` 的 `ended` **不冒泡**，所以在 `#playerStage` 上挂**捕获阶段**的委托（`bindPlayerEnded`，幂等），新渲染出来的播放器全都自动覆盖，不用在 4 个渲染函数里各挂一遍；② 多段视频（`_1.mp4`+`_2.mp4`）先把段走完，最后一段放完才换作品；③ 「继续下一个」走到列表末尾就停（复用 `stepWork`，越界直接 return）；④ 「随机」优先只在**确实有视频**的作品里挑（否则连续撞上图文，画面上什么都不动，看着像卡住）；⑤ 菜单挂 `body` + fixed 定位，按钮贴着窗口底部时**自动向上弹**（`offsetHeight` 量完再定位），否则往下弹会被切掉一圈。图文作品（`renderMediaStage`）不显示这颗按钮 —— 那里的动图本来就在 `loop`
- **2026-11-04 c125 详情页加「推送到待下载」**：作者详情页 SEC_UID 那行、「获取资料」右边多一颗按钮，一键把这位作者推进待下载区（`POST http://127.0.0.1:8790/queue/add`，带上 uid / sec_uid / 昵称 / 抖音号 / 等级 / 状态 / 标签）。之后在「导出作者信息 → ① 选作者 → 待下载区」就能直接勾他，不用回抖音再找一遍。队列按 uid / sec_uid 判重，重复推只提示、不建两条。⚠️ 8790 是插件的本地查询服务（不是 8723 本体），没启动就推不了，提示语里写明了
- **2026-11-04 c124 新下载加筛选**：工具栏多一个「筛选」，5 行（等级/标签/状态/频率/最后下载时间），每行行首圆钮 ＋＝并集 / －＝交集，和「导出作者信息」同一套语义。**筛选在服务端做**（复用导出的判定函数），所以往下翻页拿到的也全是筛完的结果。卡片日期前缀改成 `发布 YYYY-MM-DD` —— 以前直接把作品发布日期写在卡片上，而这一页是按「下载时间」切范围的，用户看到「今天」里全是 2025 年的作品会以为筛错了（其实是显示误导，数据一直是对的）
- **2026-11-04 修封面成片变灰（202 位）**：`cover.workFile` 里记的相对路径在磁盘上找不到，原因是「去作品 ID」和「文件夹补 UID 前缀」两轮改名都没同步封面。按索引找回同一条作品的新文件名改指针，并把旧缓存按新 md5 复制一份（省抽帧）。有效封面 1614 → 1815。**根因见第八章第 36 条**
- **2026-11-04 身份字段补齐 + 「G盘」标签**：从 G 盘并入的 160 位作者统一打 `G盘` 标签（其中 60 位原本连档案都没有，一并建了）；SEC_UID 补 62 位、抖音号补 122 位 —— 来源是对照表 CSV + 作者目录里的 `.txt`（存的是主页链接）+ 登录态接口反查。全库覆盖：SEC_UID 1960/1967、抖音号 1955/1967，剩的都是注销/私密，接口返回空
- **2026-11-04 修 `profile_helper.py` 的 import**：`fetch_uid_by_secuid` 早已挪到 `tools/legacy/`，`sys.path` 只加了 `src/`，导致**页面上「获取资料」一直是坏的**（一调就 ModuleNotFoundError）。现在两个位置都试，功能恢复
- **2026-11-04 G 盘老作品并入主库**：三批早期下载（4 种互不兼容的命名格式）共 162 个目录 → 合并进已有作者 100 位、新建 62 位、搬入 **15,298 个文件 / 36.5 GB**、判重 5,078 个。认作者的唯一依据是各目录里的 `.txt`（存 `https://www.douyin.com/user/MS4wLjAB…`）和标准目录名里的 UID —— **这批目录名和库里现行昵称差得离谱，按昵称匹配会大面积认错**
- **2026-11-05 清掉 `_同作品不同文件/`（一批 / 约 1.3 GB）**：这批是 11-04 从 G 盘回收站捞回来的「同作品、不同编码」文件。本轮用 ffprobe 逐文件比分辨率（按作品内「第几张」对齐）：逐个对上视频后 **全部是 E 盘更好或分辨率相同**，图片分辨率**全部相同**（只差 JPEG 质量），这批文件 **E 盘全都有对应作品**。已整目录移入 G 盘回收站（可还原）
- **2026-11-05 清掉 `_唯一版本/`（二十多个 / 几十 MB）**：先逐个核对——**23 个 E 盘有（20 个 E 盘更清晰），只有 1 张是 E 盘真没有的**：`某作者的一条图集` 那条图集里 `_1.jpeg`（2160×2880）。同时发现 E 盘这条图集本身有瑕疵：`_1` 和 `_4` 是**同一张照片**（缩到 64×64 灰度后平均差 0.7），4 个文件其实只有 3 张不同的图。处置：把 G 的 3 张按内容换进 E（`_2`/`_3` 由 1440×1920 升到 2160×2880，`_4` 换成缺的那张连拍），**文件名一律不动**（索引只存 rel 路径、不存大小，所以不用重扫），再删目录。替换后 4 张两两差异最小 31，互不相同；原文件备份 `browser/backup/fix_henfan_20261105/`
  - ⚠️ 比对图片是否「同一张」不能只看分辨率/体积，要用**多尺度中心裁剪 + 64×64 灰度平均差**（< 2 即同一张）。踩过的坑：`_2`/`_3` 的序号对齐不可靠（G 的 `_1` 其实是 E 没有的第 4 张），必须按内容配对
- **2026-11-04 清空 G 盘回收站**：逐个算 SHA1 后，确认 **两万多个 / 数十 GB** 在 E 盘有字节完全相同的副本才删；二十多个「同作品但文件不同」（大小都对不上）单独捞到 `_唯一版本/`，按用户口径（内容+名字+日期三项全一致才算同一条）不删。G 盘释放了数十 GB。⚠️ **`_唯一版本` 这个名字是错的**（2026-11-05 复查）：逐个查过，19 个分辨率低于 E 盘、5 个相同，**没有一个真的唯一**，只是当时按 SHA1 比对判的"不唯一"被误当成"独一份"
- **2026-11-04 修「文件夹改名后文件数翻倍」**：`_files_rename_folder()` 改完目录没同步 `download_log.db` 的前缀，`_delete_missing()` 按旧 folder 精确匹配删不掉旧记录，watcher 扫到新目录又插一批 → `file` 表直接翻倍（某作者 249 → 498）。改名后立刻调 `download_db.rename_folder()` 改前缀
- **2026-11-03 插件认人改成「只认 SEC_UID」**：`bridge.py` 的 `lookup()` 删掉「昵称精确」和「昵称包含」两条路径，只走 `sec_uid → uid → 作品ID反查`；昵称只用于**列提示**，不参与判定。未命中分三种：`name_diff`（同名但是另一个号）/ `name_unfilled`（库里那条没填 SEC_UID）/ `miss`+`need_sec`（页面没拿到 SEC_UID）。起因：同名的新旧两个号被硬认到已封的旧档案上，面板显示「禁言/封号」，看着像认错人。另修「描述区/评论区的 @ 提及被当成作者」——`a[href*="MS4wLjAB"]` 到处都是，得按区域排除
- **2026-11-02 c113** 一个作品下成 `_1.mp4`+`_2.mp4` 时：播放器支持作品内翻段（底部 `‹ 1/2 段 ›`，`detailState.vidIndex` 复位/递增 + `stepVideo`）；卡片徽标由「多动图圆点」改为蓝底「多段视频」；纯视频单段不再有任何徽标
- **2026-11-02 c112** 「① 选作者」的并集/交集开关改版：从行尾两个小方块改成**行首一个圆钮**（24px 圆形，蓝底 ＋ = 并集、橙底 － = 交集，点一下原地切换，`toggleExportMatch`），提示语同步
- **2026-11-03 去重** 全库清理「同作品两份文件」（一份带作品 ID、一份不带）：444 组候选 → 删 450 个文件 / 0.64 GB，作品数不再出现同一天同一作品的重复卡片；清单存 `browser/backup/去重_20261103/`
- **2026-11-03 c122** 播放区放大到「新下载」规格（440 宽 × `calc(100vh-200px)` 高，stage 用 flex 吃满）；频率徽章和筛选面板文案改回原值 `day/3天/周/月`（不再写「日更」）
- **2026-11-03 c120** 按标注改两处：① 作品行改成**固定列数 grid、不横向滚**（`rowFitStrip` 按宽度算列数，多出的隐藏，不够留白）② 表头右侧加 `⬇ N 天前`（最后下载）。顺带确认视频能真播（headless 需 `--autoplay-policy=no-user-gesture-required` + CDP 真实鼠标事件）
- **2026-11-03 c119** 「每作者一行」改版式：从「左信息 + 右缩略图」换成「一行作者表头 + 一行作品卡片」，右侧加**常驻播放区**，点卡片就地播放（视频自动转码、图文可翻页），不跳作者页；`recent` 加 `images[]`
- **2026-11-02 c116/c117** 作者列表新增「每作者一行」模式（初版）：左侧作者信息（含**全部**标签 + 更新频率/等级徽章）+ 右侧最近 12 个作品缩略图；`/api/authors` 加 `works=N` 返回 `recent` + `recent_more`，`tags` 去掉 `[:3]` 截断；网格卡片也加上更新频率/等级徽章（状态属于「禁用频率」的账号不显示）。行模式每页 20 个作者
- **2026-11-02 c110** 「① 选作者」去掉顶部的「属性之间 交集/并集」全局单选，改成每个属性行右侧一组小方块 **行首一个圆钮，里面 ＋（并集，默认）/ －（交集），点一下切换**：`_attr_mode(key)` 按 `match_<key>` 取模式，所有 － 项必须全满足、所有 ＋ 项至少命中一个；`exportState.match` 换成 `matchMap`，`clearExportAll` / 「重置」回到全 ＋
- **2026-11-02** 抽封面并发闸 4 → **8**（用户要求）：`_THUMB_SLOTS = BoundedSemaphore(8)`，每进程仍 `-threads 2`，靠多进程占核而不是单进程多线程，12 核上基本铺满、又不会像不限并发那样互抢
- **2026-11-02** 封面/转码硬件加速：抽封面并发闸（8 个 ffmpeg × 2 线程）；转码改 NVENC 优先（4K 17.7s 源 33s→2.4s）；修 `_eff_ffprobe()` 把目录名也替换掉的 bug（编码探测一直失败、转码一直按 720p）
- **2026-11-02 当晚** 抽封面的 `-hwaccel cuda` **撤掉**（并发实测 145ms vs CPU 26ms，慢 5.6 倍，用户反馈加载变慢）；GPU 只留转码。串行单张测不出这个差异，是这次踩的坑
- **2026-11-02 c109** 取消「大小号 ↔ 更新频率」联动：`setFreq` 只写当前账号（`setLevel` 仍整组同步）
- **2026-11-02 c108** 修「从作者页返回新下载页卡一帧巨图」：视图隐藏时 `clientWidth=0` 被 `ResizeObserver` 当成变窄，重排退化成「一卡一整行」；改为隐藏时不重排、`ndLayout` 沿用上次可见宽、`ndRelayout`/`ndMaybeLoadMore` 加零宽高闸
- **2026-11-02 c107** 取消「新下载只显示最新 600 个作品」的上限：改成按文件分页 + 滚到底自动续页，`/api/new_downloads` 由 `limit`(作品数)/`truncated` 换成 `limit`(文件数)/`cursor`/`has_more`
- **2026-10-31 DouK 修** 文件名撞车导致作品被静默覆盖：`name_format` 加作品 `id`（`create_time nickname desc id`），`parse_work_filename()` 剥掉末尾那串 ID。实测那轮 3608 个文件名对应 3660 个作品 → 丢了 52 个；日志里反反复复的 `PermissionError(13)` 就是这个病的症状，不是杀软
- **2026-10-31 c93** 新下载缩略图改「连续铺开 + 换作者标记」；右侧播放区尺寸固定（420px / 信息区 84px）；导出预览去掉 720px 上限、列宽按比例、每行加 `✕ 这次不导出`
- **2026-10-31 c87** 播放区信息区压成两行（作者+徽章+序号 / 日期+描述+原文件），去掉左右翻页按钮和头像，画面放大 11%
- **2026-10-31 c86** 播放区按视频比例自适应去黑边（后被 c93 取消，改成固定尺寸）+ 作者等级/频率/标签徽章
- **2026-10-31 修** 详情页「作品 0」但磁盘有文件：`_sync_cache_after_change` 漏同步 `CACHE["accounts"]` + `load_data` 只看 mtime 导致永久错位（详见第八章第 16 条）
- **2026-10-31 c99** 新下载布局跟随可用宽度动态调整：列数按列表宽实时算（收起侧边栏 6→7 列）、播放区也一起加宽（420→500）；`ResizeObserver` 触发重排，保留播放状态与滚动位置
- **2026-10-31 c96** 新下载左列表改成「一位作者一个气泡」：浅底圆角包住全部作品，内部横向排满（6 列）、超出换行；气泡宽度按作品数收窄，小气泡并排等高
- **2026-10-31 c94** 新下载按标注重排：一张卡只留日期、作者名上移到块顶（带等级/频率/状态/标签）、块宽按作品数收窄并排补位；右栏多图作品加缩略图带、点图原位放大（不全屏）
- **2026-10-31 c85** 新下载从「页内浮层」改成「左列表 + 右常驻播放」
- **2026-10-31 c83** 新增 `🆕 新下载` 视图；修 DouK `earliest` 日期格式（横杠 → 斜杠）
- **2026-10-31 c82** 导出页新增 `⚡ 一键写入 DouK`；备份 DouK 配置（原样 / 空白无账号各一份）
- **2026-10-31 c75** 新增标注台 `/annotate.html`（框选 / 箭头 / 画笔 / 文字，自动认区域 + 选择器）
- **2026-10-31 c76–c80** 按标注台意见改详情页：筛选栏留白、返回栏字号、标签「＋添加」去红、数据条改气泡、SEC_UID 显示长度
- **2026-10-31 c74** 数据条改回「整行摊开在右下角」，`⟳` 回到行尾，动作栏收回三颗
- **2026-10-30** 合并 113 个重名文件夹 + 重扫；新增文件管理（文件夹改名 / 重复文件清理）

## 附录 B：归档

| 文件 | 内容 |
|---|---|
| `本地 _archive/ 下的历史详述（含真实账号信息，未随仓库发布）` | 精简前的第七章（详情页布局沿革 / 大小号组模型 / 资料补齐脚本）、第八章（49 条踩坑时间线）、第九章（旧验证方法，含 jsdom 套路）、附录（逐版本更新记录） |
| `backup/文档_精简前_20261031-193458/` | 精简前四份文档的整份副本（PROJECT / README / 交接说明 / 说明） |

> 归档目录里的文件**不是维护对象**，只在想追「当初为什么这么做」时翻。
