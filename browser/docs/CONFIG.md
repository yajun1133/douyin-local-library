# 配置与接口速查（CONFIG）

> 这份文档回答三个问题：**哪些东西可以配**、**配在哪**、**改完怎么验证**。
> 接口的完整参数表在 [`PROJECT.md`](PROJECT.md) 第三节，这里只给最常用的调用示例和错误约定。

---

## 一、进程与端口

| 进程 | 入口 | 端口 | 绑定 | 说明 |
|---|---|---|---|---|
| 本地管理后台 | `browser/src/server.py` | **8723** | `127.0.0.1` | 网页 + 媒体 + 全部 `/api/*` |
| 插件查询服务 | `douyin-author-marker/local-service/bridge.py` | **8790** | `127.0.0.1` | 只读查询 + 待下载队列 |

两个都**只监听回环地址**，局域网和外网访问不到。

启动：

```bat
start-all.bat            :: 两个一起起，最后回报存活状态
browser\start-browser.bat    :: 只起 8723
douyin-author-marker\local-service\start-service.bat  :: 只起 8790（前台）
douyin-author-marker\local-service\start-silent.vbs   :: 8790 静默启动（无窗口）
```

停止：`stop-all.bat` / `local-service\stop-service.bat`。

查存活（这条命令本机常用，**必须带 `--noproxy`**，否则会被系统代理转成 502）：

```bat
netstat -ano | findstr ":8723 "        :: 看有没有 LISTENING
curl --noproxy 127.0.0.1 http://127.0.0.1:8723/api/meta
curl --noproxy 127.0.0.1 http://127.0.0.1:8790/health
```

---

## 二、`src/paths.py` —— 全项目唯一路径源

任何脚本、任何 bat **都不应该再自己拼路径**。换机器只改这一段（文件里标了「外部路径」注释块）。

| 常量 | 含义 | 换机器要改 |
|---|---|---|
| `MEDIA_BASE` | 下载器根目录 | ✅ |
| `SCAN_ROOT` | 作品根目录 = `MEDIA_BASE\downnnnnnnn`，扫描/播放/删除都限定在这里 | 跟着 `MEDIA_BASE` 走 |
| `PYTHON` | 跑 `server.py` / `scanner.py` 的解释器（3.13） | ✅ |
| `PYTHON312` | 跑 `douk_config.py` / `profile_helper.py` / `fetch_favorites.py` 的解释器 | ✅ |
| `FFMPEG` / `FFPROBE` | 抽帧、转码 | ✅ |
| `DOWNLOADER_DIR` | DouK 下载器目录（含 `lib/`、`Volume/settings.json`） | ✅ |
| `PORT` | 8723 | 端口冲突时改 |

派生目录（**不要单独改**，都由 `APP_ROOT` 推出来）：

```
browser/
├─ data/          data.json / user_data.json / download_log.db / settings.json / 回收站.json
├─ cache/         thumb_cache（可删）/ trans_cache（可删）/ cover_cache（永不删）
├─ backup/        标注与下载器配置的自动快照
├─ export/        导出的 csv / txt
├─ recycle-bin/   删除作品的回收站（跨盘时是真实复制，见 AUDIT）
└─ docs/          本目录
```

> ⚠️ `PYTHON` 现在是 `C:\Users\...\.workbuddy\binaries\python\versions\3.13.12\python.exe`。
> 客户端升级换了版本号，这个路径就会失效。**`paths.py` 和 `browser/start-browser.bat` 里各有一份**，
> 换环境时两处都要改（`start-all.bat` 有 `python` 兜底，`start-browser.bat` 没有）。

---

## 三、`data/settings.json` —— 界面里能改的配置

由「设置」页写入，服务端 `load_settings()` / `save_settings()` 维护。**文件不存在就全用默认值**，
损坏时也回退默认值（不抛异常）。写盘是「临时文件 + `os.replace`」原子替换。

| 键 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `update_freq_disabled_status` | `string[]` | `["注销","禁言/封号","卖号"]` | 这些状态的账号不允许有「更新频率」。保存时会**真正清掉**已设的频率 |
| `fav_latest_mode` | `"none"` / `"downloaded"` / `"all"` | `"none"` | 收藏页「最新发布」的口径：用收藏那条的时间 / 只查已下载作者 / 全都查 |
| `paths.data_dir` | `string` | `""` | `data.json` 所在目录。空 = 用 `paths.DATA_DIR`。**重启生效** |
| `paths.ffmpeg` | `string` | `""` | 覆盖 ffmpeg 可执行文件，ffprobe 由文件名派生 |
| `paths.downloader_dir` | `string` | `""` | 覆盖 DouK 目录（抓收藏 / 改下载器参数用） |

示例：

```json
{
  "update_freq_disabled_status": ["注销", "禁言/封号", "卖号", "不好看了"],
  "fav_latest_mode": "downloaded",
  "paths": {
    "data_dir": "",
    "ffmpeg": "D:\\app\\ffmpeg\\bin\\ffmpeg.exe",
    "downloader_dir": ""
  }
}
```

校验规则（`save_settings()`）：

- `update_freq_disabled_status` 只接受 `ALL_STATUS` 里的值，重复项自动去重，旧值自动映射
  （`停更/偶尔更` → `停更/偶更`）；
- `fav_latest_mode` 只接受三个枚举，其它值忽略；
- `paths.*` 只接受字符串，自动 `strip()`；不校验路径是否真实存在。

### 3.5 `data/watch.json` —— 📡 追更的名单与节奏（**不在 settings.json 里**）

两份文件分工明确，别放错：

| 文件 | 内容 | 丢了会怎样 |
|---|---|---|
| `data/watch.json` | `authors[]`（`uid` / `sec_uid` / `nickname` / `enabled` / `interval` / `added_at` / `full_scan`）、`settings{}`、号池 `accounts[]` | **丢名单**——得重新一个一个加。详情页那条横栏和追更面板读写的是**同一份** |
| `data/watch_state.json` | 每位作者的 `seen[]`（判重用）/ `last_ts` / `next_check` / `tier` / `fails` / `downloads` / `last_scan`，以及 `bulk` / `bulk_more` / `full_done_for`（全量补齐的进度锚点） | 只是进度：会从「本地最新作品」重建基线，最多把已有的当新作品查一遍（不会重下，下载前按四层判重挡住） |

`authors[]` 里两个字段专门管「全量补齐」：`full_scan:true`（加入时写入，表示还没全量扫过）+ `added_at`
（`full_done_for == added_at` 才算做完，所以**同一位作者移出再加一次 = 重新全量扫一遍**）。

`watch.json.settings` 默认值（`watch_core.DEFAULT_CFG`，面板上都能改；括号里是本机当前值）：

| 键 | 默认 | 作用 |
|---|---|---|
| `interval_hot/warm/cold/idle` | 900 / 3600 / 10800 / 86400 秒 | 热/温/冷/静四档间隔，`interval:0`（自动）时按这位作者多久没发过作品自动挑档 |
| `hot_days` / `warm_days` / `cold_days` | 2 / 7 / 30 天 | 三档的分界线 |
| `per_author_pages` | 1 | 常规检查翻几页（一页 20 条，1 页够覆盖绝大多数日更） |
| `backfill_pages` | 4 | 关机回来发现「整页都比基线新」时，往下补翻几页 |
| `bulk_pages` | 10 | 本地**零作品**的作者首次补齐翻几页（≈200 条） |
| `full_pages` | 40 | 「全量扫一遍」（新加进追更）翻几页（≈800 条）；翻满未到底由 `bulk_more` 接力 |
| `gap` / `gap_jitter` | 2.0 秒 / 0.3 | 每个作者之间的间隔与抖动（防风控，别低于 2） |
| `daily_budget` | 20000（**80000**） | **每个号**每天的请求上限，实际总额度 = 该值 × 可用号数；到了当天就停 |
| `rest_every` / `rest_secs` | 100 / 20 | 每查 N 位歇 N 秒 |
| `break_fails` / `break_cool` | 5 / 600 | 连续失败几次进冷却、冷却多久 |
| `net_wait` / `net_cool` | 20 / 120 | 网络类失败（断网/代理）原地等多久、连着失败几次停多久（**不封号**） |
| `account_ban` | 300 | 单个号被限流后歇多久再启用 |
| `max_per_round` | 40（**80**） | 单轮最多下几条作品，超了下一轮接着下 |
| `content` | `all` | 只追 `video` / `image`；被跳过的那类照样推进基线 |
| `desc_len` / `fail_limit` / `seen_keep` | 64 / 3 / 400 | 文件名描述截断长度（与下载器一致）/ 连续失败几次先放弃 / 每位作者记住多少条已见作品 |
| `no_api_drop_rounds` | 3 | 连续几次扫不到才打「扫不到」标签 + 取消追更（判过之后 12 小时内只打一枪） |

⚠️ 名单里**认人只认 `sec_uid` 和 `uid`，昵称不参与判定**（同名号太多、还会改名换号）。加人时查不到 SEC_UID 会被直接拒绝——加进去也下载不了。

---

## 四、环境变量

| 变量 | 谁读 | 作用 |
|---|---|---|
| `SKIP_FOLDER_MERGE=1` | `scanner.py` → `folder_merge.auto_merge()` | 跳过「同 uid 多文件夹自动合并」 |
| `DY_BRIDGE_PORT` | `bridge.py` | 临时改 8790 端口（调试用） |
| `PYTHONPATH` | 全部 | **启动脚本里必须清空**。某些启动器会注入「安全删除」钩子，一次删超过 50 个文件就 abort 进程 |
| `CODEBUDDY_SAFE_DELETE_ENABLED=0` | 同上 | 关掉上面的钩子 |
| `NO_PROXY` / `no_proxy` | 全部 | **必须含 `127.0.0.1,localhost`**，否则自检请求走代理拿到 502，会被误判成「服务没起」 |

这三个（`PYTHONPATH` / `CODEBUDDY_SAFE_DELETE_ENABLED` / `NO_PROXY`）在 `start-browser.bat`、
`start-all.bat` 里都已设好。**如果你换别的方式启动（比如 IDE 里直接跑、后台任务跑），必须自己带上**，
否则会出现「每个请求 502」和「一删作品进程就消失」两种怪故障。

---

## 五、`douyin-author-marker/local-service/config.json`

不在仓库里（`.gitignore` 已排除），仓库只提供 `config.example.json`。第一次用把 example 复制成
`config.json` 再改路径。

| 键 | 默认 | 说明 |
|---|---|---|
| `port` | `8790` | 监听端口 |
| `bind` | `"127.0.0.1"` | **不要改成 `0.0.0.0`**，会把作者库暴露到局域网 |
| `download_db` | 绝对路径 | 指向 `browser/data/download_log.db`（只读打开） |
| `user_data` | 绝对路径 | 指向 `browser/data/user_data.json`（只读） |
| `remote_latest` | 绝对路径 | 指向 `browser/data/remote_latest.json` |
| `admin_base` | `http://127.0.0.1:8723` | 「＋待下载」里新建作者时调谁 |
| `queue_file` | `"pending-queue.json"` | 待下载队列（相对 local-service 目录） |
| `recent_per_author` | `12` | 每位作者在索引里保留多少条最近下载记录 |
| `build_aweme_index` | `true` | 建「作品 ID → 下载时间」索引（判断当前视频是否已下载） |
| `auto_reload` | `true` | 每 60 秒看一次数据文件 mtime，变了就重建索引 |
| `verbose` | `true` | 打印日志（`pythonw` 启动时写进 `服务日志.log`） |

> 这里全部是**绝对路径**，项目目录一挪就要跟着改。这是 bridge 最脆弱的一处配置。

---

## 六、模块清单（各自只干一件事）

### `browser/src/`（主程序，Python 3.13）

| 文件 | 职责 | 单独能跑吗 |
|---|---|---|
| `server.py` | HTTP 服务：网页、媒体、全部 `/api/*`、回收站、文件管理、批量任务 | ✅ `python src/server.py` |
| `scanner.py` | 扫 `downnnnnnnn` 生成 `data/data.json`（增量，只扫 mtime 变了的文件夹） | ✅ `python scanner.py [--full]` |
| `watcher.py` | `ReadDirectoryChangesW` 监听下载目录，攒批后通知扫描 | 作为模块被 server 用 |
| `download_db.py` | SQLite 下载记录库（`data/download_log.db`），「多久没下载过」全靠它 | ✅ 直接跑打印总量 |
| `folder_merge.py` | 同一 uid 多个文件夹的自动归一（`os.rename`，不复制） | ✅ `python folder_merge.py [--apply]`（默认 dry-run） |
| `paths.py` | 唯一路径源 | ✅ 打印当前生效路径 |
| `douk_config.py` | 读写 DouK 的 `settings.json` / pyc 常量 | ✅ 需 **3.12**，stdin 收 JSON |
| `profile_helper.py` | 登录态查资料（常驻子进程） | ✅ 需 3.12 |
| `latest_helper.py` | 查「最新一条作品发布时间」 | ✅ 需 3.12 |
| `fetch_favorites.py` | 抓收藏 / 喜欢列表 | ✅ 需 3.12 |
| `query_uid_by_secuid.py` | sec_uid → uid / 昵称 / 抖音号 | ✅ 需 3.12 |
| `watch_core.py` | 📡 追更核心：轮询 `aweme/post` → 判重 → 直连 CDN 下载 → 落 `data/watch_state.json`；另含号池（`pool_*`）、节奏分档、额度/熔断 | 被 server / watch.py import |
| `watch.py` | 追更的 CLI 与常驻进程（`--list / --status / --add / --add-top / --remove / --scan / --dry / --loop`） | ✅ `python src/watch.py --status`（需 3.13） |

### `browser/tools/`（独立小脚本，不起服务也能跑）

| 文件 | 用途 |
|---|---|
| `blacklist.py` | 黑名单公共模块（被 server import） |
| `block_author.py` | 把博主加入「不下载」名单 |
| `clean_cache.py` | 清缩略图 / 转码缓存（不碰封面） |
| `clean_index.py` | 把混进 `data.json` 的标注字段剥掉 |
| `csv_to_douk.py` | 网页导出的作者 csv → DouK 账号列表 JSON |
| `douk_patch_suspend.py` | 改 DouK 里写死的等待参数（界面上没有） |
| `downloaded.py` | 「已下载博主」公共模块，以下载目录为唯一依据 |
| `fill_secuid_from_export.py` | 从本地导出表补 sec_uid（不联网） |
| `backfill_author.py` | 按作者补历史作品（不看追更基线，拉主页逐条判重后补下）；**跑前先停追更常驻** |
| `missing_of_author.py` | 对账：拉一位作者全部作品，走四层判重，只列「本地缺哪几条」；`--apply` 才真下。追更说「下过了」可它就是不下来时用它 |
| `backfill_watch_downloads.py` | 从 `watch.log` 回填追更下载明细（判据：日志时间 == 文件 mtime，精确匹配） |
| `selftest_watch.py` | 追更自检（不联网、不碰真实状态，用假数据撞边界；改 `watch_core.py` 后**必跑**） |
| `probe_latest.py` | 诊断：打印作者主页 `aweme/post` 原始返回 |
| `restore_backup.py` | 从 `backup/` 恢复 `user_data.json` |
| `secuid_check.py` | 体检：找出 sec_uid 指向了别人的账号 |
| `bump_version.py` | 一处改版本号，`index.html` 两处 `?v=` 同步 |
| `legacy/` | 历史脚本，**归归档不维护** |

---

## 七、常用接口示例

完整参数见 `PROJECT.md` 第三节。以下都是可以直接粘进 cmd 的（注意 `--noproxy`）。

```bat
:: 1) 总体统计
curl --noproxy 127.0.0.1 "http://127.0.0.1:8723/api/meta"

:: 2) 作者列表第一页，按作品数倒序
curl --noproxy 127.0.0.1 "http://127.0.0.1:8723/api/authors?offset=0&limit=20&sort=work_count&dir=desc"

:: 3) 搜索（只在昵称范围里搜）
curl --noproxy 127.0.0.1 --get --data-urlencode "q=阿" --data-urlencode "scope=nickname" ^
     "http://127.0.0.1:8723/api/authors"

:: 4) 单个作者详情（含 works）
curl --noproxy 127.0.0.1 "http://127.0.0.1:8723/api/author?uid=前面列表里拿到的uid"

:: 5) 保存标注（合并写：只给要改的字段，没给的保持原样）
curl --noproxy 127.0.0.1 -X POST -H "Content-Type: application/json" ^
     -d "{\"accounts\":{\"123456\":{\"starred\":true,\"tags\":[\"示例\"]}}}" ^
     "http://127.0.0.1:8723/api/save"

:: 6) 触发重扫（立即返回，进度看 /api/rescan_status）
curl --noproxy 127.0.0.1 -X POST "http://127.0.0.1:8723/api/rescan"

:: 7) 最近下载（今天 0 点之后）
curl --noproxy 127.0.0.1 "http://127.0.0.1:8723/api/new_downloads?since=0&limit=50"

:: 8) 回收站列表
curl --noproxy 127.0.0.1 -X POST -d "{}" "http://127.0.0.1:8723/api/recycle/list"

:: 9) 插件服务：按 sec_uid 查本地有没有
curl --noproxy 127.0.0.1 "http://127.0.0.1:8790/lookup?sec_uid=MS4wLjABAAAA..."

:: 10) 插件服务：健康检查（含作者的索引规模）
curl --noproxy 127.0.0.1 "http://127.0.0.1:8790/health"
```

### 错误响应约定

- 写接口（POST）**统一**返回 `{"ok": true|false, ...}`；失败时带 `error` 字符串，HTTP 状态码
  一般是 400 / 404 / 409 / 500。
- 读接口（GET）**正常**时直接返回业务对象（`/api/authors` 返回 `{total, offset, items}`，
  `/api/meta` 返回 `{app, total_authors, starred, tags, years}`），**没有** `ok` 包装。
- **GET / POST 都有统一异常兜底**（2026-11-06 起）：接口内部抛异常时统一回
  `{"ok": false, "error": "异常类型: 信息"}` + HTTP 500，同时在服务窗口打印完整 traceback。
  前端只会 toast「加载失败」，**真正原因看服务窗口**（或 `curl` 一次复现）。
- `_instance_alive()` 靠 `/api/meta` 里的固定字段 `"app": "douyin-local-library"` 判断
  「8723 上跑的是不是本程序」，不再依赖键的顺序。

### 安全边界（改代码时别拆）

- `safe_media_path()` 是所有「前端给相对路径」的唯一入口：挡 `..`、绝对路径、盘符、NUL，
  最后用 `realpath + commonpath` 校验必须落在 `SCAN_ROOT` 内。
- `_client_ok()` 校验 `Host` 和 `Origin`：挡跨站 POST 与 DNS rebinding。**新增接口也要走它**
  （`do_GET` / `do_POST` 开头已经统一调用）。
- 静态文件是**白名单**（`STATIC_FILES`），`/user_data.json` 单独映射，其余一律 404。
  不要改回「文件存在就发」。

---

## 八、改完怎么验证（最小自检）

```bat
:: 1) 语法
python -m py_compile browser\src\server.py
node --check browser\src\web\app.js

:: 2) 路径是否指对
python browser\src\paths.py

:: 3) 服务是否活着
curl --noproxy 127.0.0.1 http://127.0.0.1:8723/api/meta

:: 4) 索引与磁盘是否一致（作者数、作品数、缺 sec_uid 数）
curl --noproxy 127.0.0.1 http://127.0.0.1:8723/api/data_health
```

改前端（`web/` 下任何文件）后**必须**把 `index.html` 里的 `?v=` 版本号往后挪一位，
否则浏览器还用旧缓存，看起来像「改了没反应」（`tools/bump_version.py` 可以一键改）。
