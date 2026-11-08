# 交接说明 —— 给下一个模型 / 下一个对话

> **一句话**：本地抖音下载内容浏览器的**网页壳**。视频图片早就下在 E 盘了，文件夹名全是 UID，这个项目给它们套一层可浏览的界面。
>
> 本文只回答「**怎么接手**」。技术细节（每个接口的参数、每个坑的根因）在 `docs/PROJECT.md`；面向用户的操作说明在 `GUIDE.md`。
>
> 最后更新：2026-11-01 19:50 · 前端 `?v=20261031c102`（本文档已精简，历史细节已移出仓库，本地保留在 `_archive/`）

---

## 0. 三十秒速览

| | |
|---|---|
| 项目根目录 | `D:\ai_Project\douyin-local-library\browser\` |
| 数据源（只读） | `E:\dyyyyyyyyyyyyydownload\TikTokDownloader_V5.6_Windows_X64\downnnnnnnn`（约 1700 个 `UID<uid>_<昵称>_发布作品` 文件夹 + 2 个收藏夹；2026-10-30 已合并改名产生的 113 个旧名文件夹） |
| 技术栈 | Python `ThreadingHTTPServer` + 原生 HTML/CSS/JS。**无框架、无构建、无数据库、无 AI 依赖** |
| 端口 | `http://127.0.0.1:8723` |
| 启动 | 双击 `start-browser.bat`，或 `python src\server.py`。**已在运行时双击不会再起第二个进程**（脚本先 curl `/api/meta` 探测，活着就只开浏览器并退出） |
| 数据量 | 现查 `/api/meta`（2026-11-04：1961 位作者 / 288,718 个作品 / 386,726 个文件 / 索引约 86MB） |
| 不可再生的数据 | **只有 `data\user_data.json`（用户的标注）**，其余全可重建 |

**最容易踩的六条**（详见第 4 节）：
1. 改了 html/js/css **必须升** `index.html` 里的 `?v=` 版本号，否则浏览器用旧缓存，看起来「改了没反应」。
2. 写 `user_data.json` **只能走 `merge_save_user_data()`**，直接 `open("w")` 会整库覆盖（真出过事故，见第 5 节）。
   **前端改字段必须 `markDirty(uid, '字段名')`**（多个字段用 `markDirtyFields`）。保存只发改动字段，不传字段名会退回「整条账号上送」——那就是旧快照覆盖新数据的老坑（2026-11-08 c153 修的就是这个）。
3. 前端**永远不要** fetch `data.json`（约 72MB）。
4. 标注字段（星标/标签/封面/大小号…）的**权威只有 `user_data.json`**，靠服务端 `load_user_data()` 叠加进内存。
   - 历史坑（2026-11-01 已修）：`CACHE["accounts"]` 和 `data.json` 的账号是同一批 dict，五个写盘口会把标注一起 dump 回 `data.json`。现在写盘统一过 `_index_only()`，剥掉 10 个标注字段；老残留用 `python tools\clean_index.py` 清（`--dry` 先看）。
   - 仍要守：**别拿 `data.json` 当标注备份或恢复源**，详见 `PROJECT.md` 第二章。
5. 加设置项：**只在 `DEFAULT_SETTINGS` 里加一行就够**，`load_settings()` 按类型通用合并。别再去读盘那段逐个 `if` 手写——`fav_latest_mode` 就是这么丢的（写了读不回来，2026-10-30 修）。
6. 本机 ffmpeg 是 **LGPL 构建，没有 libx264**，转码只能用 `libopenh264` / `h264_mf`。**但 2026-10-28 起本机装了 Windows「HEVC 视频扩展」，Chrome 已能直接硬解 HEVC——正常情况根本不会触发转码**，只有换机器没装扩展、或个别文件原生解不动时才走到这条路。

---

## 1. 现在的状态

数字天天变，**不要写死，用时现查**：

```bash
curl -s --noproxy 127.0.0.1 http://127.0.0.1:8723/api/meta
```

- 作者数 / 星标 / 标签云 都在这个接口；口径说明见 `PROJECT.md` 第一章
- 2026-11-04 核对：作者 **1961** · 星标 **119** · 前端 **c124** · `node --check`（node v26.13.0，已在 PATH）与 `py_compile` 均通过
- 2026-11-04 的改动集中在：新下载筛选（c124）、G 盘老作品并入（15,298 个文件）、封面指针修复（202 位）、身份字段补齐。细节见 `PROJECT.md` 附录 A 前几条
- **2026-11-06 新增「📡 自动追更」**（前端 c143）：不经过 DouK，按作者轮询抖音、发现新作品自己下。**入口只有两处，改的是同一份 `data/watch.json`** —— ① 作者详情页信息卡下面那条横栏（开关 + 间隔 chips）；② 左侧「功能」栏的「📡 追更」视图（`#watchView`），名单**每一行都能点，点了在应用内进作者详情页**（`in_lib=false` 的作者只提示不跳）。核心在 `src/watch_core.py` + `app.js` 末尾「追更（watchView）」段，接口 `/api/watch`、`/api/watch/status`。改了前端记得 `bump_version`。**2026-11-08 c145 起追更不再是独立页面**：`src/web/watch.html` 已删，`GET /watch` 只是 302 到 `/#watch`（保旧书签）；名单筛选改成作者详情页那套分段控件（`全部/热档/温档/冷档/静档/定档/有下载/已停用`，单选、带数量）。详见 `PROJECT.md` 七.6 和 `CONFIG.md` 3.5
- **2026-11-07 追更判重修省略号 + 新增 `tools/backfill_author.py`**：老文件名把长描述中段省略成 `#...`（全库约 2.9 万条），接口给的是完整描述 → 全量扫老作者必重复下载。判重改成「精确命中 → 省略号前后缀双向模糊（限定同一天、前后缀总长 ≥6）」，函数在 `src/watch_core.py` 的 `is_known()` / `ellipse_split()` / `build_date_index()`，细节见 `PROJECT.md` 七.6「判重三层」。要补某位作者历史作品别用追更（有基线），用 `python tools/backfill_author.py <uid> --pages 12`，跑前先停追更常驻
- **2026-11-08 追更失败是代理不是账号**：面板上「抖音那边连续失败，已自动暂停 N 分钟」先去看 `data/watch.log` —— 如果是 `curl: (7) Failed to connect ... over proxy 127.0.0.1`，就是常驻进程继承了已失效的会话代理端口，不是被限流、也不是 cookie 过期。`watch_core` 已用 `NO_PROXY_OPTS` 强制绕过代理，网络类失败走 `RetryLater`（不封号、不熔断，20 秒重试）。**改这一块的规矩：任何新加的 curl_cffi 请求都要带 `**NO_PROXY_OPTS`**
- **2026-11-08 图文作品的下载口径（实况 / 动图 / 画质 / 判重）**：`images[]` 每一项都可能带 `video.play_addr` —— **判据永远是「这一项有没有 `video`」，别看 `aweme_type`；顶层 `aweme.video` 是整组图的轮播视频，不能下**。实况和动图走同一条路（下 `_N.mp4`）；后缀改由**服务器 `Content-Type`** 定，不按 URL 猜；`url_list` 取**最后一个**（前几份是 9 KB 的 webp 缩略图，最后才是原图 —— DouK 的 `IMAGE_INDEX = -1` 同理）；真动图（GIF / 带 `ANIM` 的 webp）转 mp4（ffmpeg 解不了动态 webp，退回 Pillow 拆帧 + `libopenh264`，转不了就保留原文件）。判重顺带修了两处，不修就会**重复下载**：命名复刻 DouK 的 `beautify_string`（中文算 2 宽、超长取头尾），`is_known()` 的后缀改拿未截断全文比、并允许「开头 ≥12 字」兜作者改过描述的情况（当天因此多下了 10 条，已删回回收站）。历史下错的跑 `python tools/backfill_live.py <uid> --repair`（同时报「缺视频」和「低画质 webp」），跑前先停追更常驻。细节见 `PROJECT.md` 七.6 的四条判据 + 判重三层
- **2026-11-08 c150 一次修一串**：① 「每作者一行」右侧播放器原来是一份**独立实现**，缺「缓存优先」和失败兜底 → 已转码过的 HEVC 会回头播原片黑屏；现在和详情页同一套取源顺序（`info.url` → `canPlayHevc()` 原片 → 转码），并补 `rowMountVideo()` 的 onerror / 5 秒兜底。② `hideAllViews()` 统一 `pause()` 全站 `video, audio` 再 `ndPlayReset()` / `rowStopPlay()` —— 播放器常驻 DOM，只 `display:none` 不会停声音。③ 侧栏开关换成顶栏左上角**常驻汉堡**（`.menu-btn`），旧的 `«`/`»` 两颗按钮删了。④ 追更页重排：加作者+账号池合成一块，名单收窄、右边放「追更内容 / 节奏与安全设置 / 运行日志」。⑤ 新增追更设置 `content`（`all/video/image`），`watch_core` 用 `aweme_kind()` 过滤，**被跳过的那类要推进 `last_ts`**，否则每轮都重新「发现」同一批
- **2026-11-08 c153 修两个「数据看着不对」的根因**（都不是数据坏了）：① **黑名单按昵称兜底会误伤同名号** —— `blacklist.match()` 会先把昵称归一化（去掉下划线/空白/星号/表情），于是「某某_」和「某某」被当成同一个人，撞上另一个同名号的黑名单行，那位作者连同 2 个真实作品被整条从内存索引剔除（列表/搜索/`/api/author` 全没了，待下载区却照样显示 2 个作品）。现在只有「黑名单行既没 uid 也没 sec_uid」才允许按昵称兜底，有 ID 的一律按 ID 精确匹配。**写文档/注释时不要把真实 UID、昵称贴进来**（仓库是公开的）。② **保存整条账号对象导致旧快照回写** —— `saveUserDataOnce()` 原来发 `acc[uid]` 整个对象，而它是页面打开时的快照，所以点个星标就能把别处改过的 `status`/`update_freq` 冲回旧值。现在按 `state.dirtyFields` **只发改动字段**（`markDirty(uid, field)`）。③ 顺手撤掉「状态落入禁用列表就 pop 掉 `update_freq`」这条无提示的销毁逻辑，频率值一律保留、只在筛选和计数时排除（`_t_freq()` 本来就这么做）。**以后加字段记得 `markDirty(uid, '新字段名')`，不传字段名会退回整条上送，等于把这个坑挖回来。**
- **没有阻塞性缺陷**，剩下的都是体验优化
- 缺 sec_uid、转码 job 丢失、死代码等已知项见 `PROJECT.md` 第五章末尾

---

## 2. 五分钟上手

### 2.1 启动 / 重启
```bash
cd /d D:\ai_Project\douyin-local-library\browser
python src\server.py            # 前台跑，Ctrl+C 停
```
需要重启时（**只有改了 `server.py` 才需要**，改前端刷新即可）：
```bash
netstat -ano | grep ":8723.*LISTENING"     # 拿 PID
taskkill //PID <pid> //F
```
⚠️ **先数一下有几个 PID**。本机踩过「端口上挂了两个 server 实例」（Windows `SO_REUSEADDR` 允许重复 bind），请求随机分配，表现成「改了没生效」。正常只有一个。
**c82 起已自动拦截**：启动前会先发 `GET /api/meta` 探测，已有实例就直接开浏览器并退出（不会再起第二份）。所以现在看到「本程序已在运行」是**正常提示**，不是报错。真要重启必须先 `taskkill`，否则新代码不会被加载。

### 2.2 改代码
| 改什么 | 怎么做 |
|---|---|
| 前端（`src/web/*`） | 改完升 `index.html` 里**所有** `?v=`（`sed -i 's/?v=20261028c50/?v=20261028c51/g'`），让用户 Ctrl+F5。**不用重启服务** |
| 后端（`src/server.py`） | 改完重启服务 |
| 路径 | 一律从 `src/paths.py` 取，别在脚本里拼 `dirname(__file__)` |
| 新增工具脚本 | 放 `tools/`，头部 `sys.path.insert(0, ../src)` 后 `import paths`。**脚本里不要写死真实 uid / sec_uid / 昵称 / 本机绝对路径** —— 参数从命令行拿，路径用 `__file__` 推导（仓库是 Public） |
| 提交 | **每次改完就 commit，不要攒**：`git add -A` → 提交信息写清「为什么改 + 怎么验的」→ `git push origin main`（能不能推先 `env \| grep -i proxy` 再 `curl -x <proxy> https://github.com/`） |

### 2.3 验证

```bash
# ① 语法
node --check src/web/app.js
python -c "import py_compile;py_compile.compile('src/server.py',doraise=True)"

# ② 接口（本机有 http_proxy，curl 必须 --noproxy，否则拿到 000/502）
curl -s --noproxy 127.0.0.1 http://127.0.0.1:8723/api/meta

# ③ 中文参数必须 urlencode，直接拼 URL 会乱码（看着像功能坏了）
curl -s --noproxy 127.0.0.1 -G "http://127.0.0.1:8723/api/authors" --data-urlencode "q=嘿嘿" --data "limit=5"
```

**界面现在能真看**：`D:/ai_Project/douyin-local-library/.workbuddy/` 下有 `shot_size.py`（可指定 CSS 视口宽高 + 局部放大）、`shot2.py`、`shot_zoom.py`，走 CDP 驱动真浏览器截图；脚本会打印 `JS 结果:`（脚本 `return` 的字符串，量尺寸就用 `return JSON.stringify({...getBoundingClientRect})`）。
⚠️ 第二个参数是 **JS 代码本身**，不是文件路径 —— 文件用 `JS="$(cat x.js)"` 传。
⚠️ JS 必须包成 IIFE：`"(function(){var r=...;return JSON.stringify(r);})()"`。`Runtime.evaluate` 执行的是表达式，**裸 `return` 会报 `SyntaxError: Illegal return statement`**。

（jsdom 那套仍可用，但只在「要断言 DOM 结构、不需要真实排版」时才用。）

### 2.4 收尾（每次改完固定三件事）
1. 升 `?v=` 版本号
2. 改 `server.py` 就重启服务
3. 同步文档：`README.md`（用户视角）、`PROJECT.md`（技术+踩坑第八节）、本文件（状态与硬约束）

---

## 3. 目录与依赖

见 `PROJECT.md` 第一章（目录树 + 外部依赖路径）。只记两条：

- **路径一律从 `src/paths.py` 取**，不要在脚本里自己拼 `dirname(__file__)`
- 换机只改 `paths.py` 里的 `MEDIA_BASE / PYTHON / PYTHON312 / FFMPEG` 四处

---

## 4. 硬约束（违反必出事，按后果排序）

### A. 数据安全（碰了就丢用户数据）

1. **写 `user_data.json` 只能走 `merge_save_user_data()`**。直接 `open("w") + json.dump` 会：整库覆盖（真出过事故）、并发丢更新、写坏文件。合并写 + `_WRITE_LOCK` + `mkstemp`/`os.replace` 缺一不可。
2. **删除字段要发显式空值**（`alt_accounts: []`、`field: null`），**不能 delete 键**——合并写遇「键不存在」会保留磁盘旧值。**删除整个账号**是 `{uid: null}`（2026-10-28 新增，配合删博主用）。
3. **前端 `user_data.json` 加载失败必须进 fatalBar**，不能静默降级成空对象；`saveUserData()` 必须检查返回值，失败要 toast。空 state 一保存就清库。
4. **封面是数据不是缓存**：图在 `cover_cache/`，**任何清理都不能碰**；改淘汰逻辑必须确认 `trim_cache(THUMB_CACHE, ..., keep=cover_cache_names())`。

### B. 前端（改了看着像没生效）

5. **改任何 html/js/css，升 `index.html` 里所有 `?v=`**。这是最高频的「假 bug」来源。
6. **滚动容器是 `.content` / `#wallScroll`，不是 `window`**。scroll 事件不冒泡，监听 window 无效（曾导致只能看到前 60 个作者）。
7. **`#detailView.hidden`、`#altView.hidden`、`#batchView.hidden` 必须单独写 CSS**。id 选择器优先级压过 `.hidden`。
8. **加新视图不止改自己的 open 函数**：`showAuthorsView()` / `openAltView()` / `openAuthor()` / `closeDetail()` 四处都要反向隐藏，`closeDetail()` 的 `state.returnView` 还要加分支。改完把 A→B→C→A 来回点一遍。
9. **侧边栏高亮清理走 `sideItems()` / `clearSideActive()`**，唯一例外是 `showAuthorsView()` 里那条仍只清 `#featureList`（它由 `applyFilters()` 调用，全清会把刚点亮的项清掉，表现成「点了没反应」）。
10. **CSS 修饰类要组合写** `.btn.btn-primary`，单写 `.btn-primary` 会被后面的 `.btn` 覆盖成白底白字。
- **视图转场约定（c54）**：恢复列表滚动位置必须瞬时设置 `scrollTop`；不要在 `.content` 上加全局 `scroll-behavior:smooth`（c53 曾因此把返回演成自上而下滑动）。整页切换统一走 `animateViewEnter(viewId, 'forward'|'back'|'neutral')`，详情返回栈和作者/大小号/批量页都按同一套方向播放。

### C. 服务端

11. **`data.json` 里没有 starred / tags / cover / alt_accounts / history / status / level**，全靠 `load_user_data()` 叠加。新增标注字段要同步两处：`server.py` 的 `load_user_data()` 和前端 `state.filtered`。
12. **`alt_accounts` 只能整组重写**（唯一入口 `writeAltGroup(members)`），它的语义是「同组其他所有人」，绕过去写成二元边，3 个以上立刻散架。改完跑 `python tools\legacy\fix_alt_groups.py --dry` 确认违规 0。
13. **长耗时操作一律「起任务 + 轮询」**，禁止在 HTTP 请求里同步等：重扫（`/api/rescan` + `/api/rescan_status`）、转码（`/api/transcode` + `/api/transcode_status`）、批量获取资料（`/api/batch_profile` + `/api/batch_status`）。
14. **批量删/改文件的循环，异常兜底必须 `except BaseException`** 且返回 `failed` 计数。用 `except Exception` 时一个文件被占用就会让整个请求变空响应，看着像服务挂了。
15. **`scanner.py` 的进度协议是 stdout 两行标记**：`PROGRESS <done>/<total>`、`PHASE <阶段>`（都要 `flush=True`），`server.py` 逐行解析进 `SCAN_STATE`。改格式要同步改解析。
16. **设置项只有一个来源：`DEFAULT_SETTINGS`**。加字段就往那加一行（`list` 或 `str` 都行），`load_settings()` 按类型通用合并读写；`save_settings()` 只校验合法性。**不要再在 `load_settings()` 里手写 `if disk.get("xxx")`**——漏一个就变成「文件里明明有值、程序重启后不认」（`fav_latest_mode` 踩过，见 PROJECT.md 第 46 条）。
17. **启动流程的每一段都要能看见**：`say()`（= `print(flush=True)`）分步打印，`start-browser.bat` 用 `python -u`。**端口检测必须发请求探测**（`_instance_alive()` 看 `/api/meta` 里有没有 `total_authors`）——Windows 的 `SO_REUSEADDR` 让重复 bind 不报错，只靠 `try/except OSError` 会起出第二个实例（见 PROJECT.md 第 48 条）。

### D. 环境（不是 bug，别去「修」）

18. **本机 ffmpeg 是 LGPL**：没有 `libx264`、没有 `cropdetect`。转码用 `libopenh264`，失败退 `h264_mf`。**但转码现在是兜底路径**：系统已装 HEVC 视频扩展，Chrome 直解 HEVC 并播原片；判断入口是 `canPlayHevc()`（`canPlayType('...hvc1...')` 返回 `probably`），换机器没装扩展就自动退回转码。
19. **查资料只能走 `profile_helper.py`（登录态）**，公开接口 `iesdouyin/web/api/v2/user/info/` 的 `signature` 恒为空串。`ProfileHelper` 是常驻子进程（别每次 Popen，启动 1~2 秒）。
20. **跨进程传中文必须走 ASCII**：3.12 子进程一律 `json.dumps(..., ensure_ascii=True)` + 父进程 `PYTHONIOENCODING=utf-8`。用 `ensure_ascii=False` 在 Windows 管道会写出 `���` 并落盘。
21. **查作品数要慢且优先免登录**：`COUNT_INTERVAL=1.6s` + 抖动、单轮 ≤400、连续 20 失败熔断。跑太密会被限流（曾 1500 个失败 1185 个）。
22. **作品目录里 `_1.jpeg` 是封面、`_2.mp4` 才是视频**，任何取文件的地方都不能盲取 `files[0]`。
23. **媒体 URL 要逐段 `encodeURIComponent`**（文件名含 `#`、空格）。

---

## 5. 数据安全：为什么是「五重防护」

2026-10-27 早上出过一次事故：`user_data.json` 从 537 条标注被覆盖成 4 条（丢了约 10 星标、100+ 封面）。**两个根因**：①前端持有全量副本，一次保存就整库覆盖；加载失败时拿空对象继续跑；②`ThreadingHTTPServer` 多线程，`/api/save` 的读-合并-写不在同一临界区，并发保存互相覆盖。

**现在的五重防护**（都已上线验证）：

| 防护 | 位置 |
|---|---|
| 合并写（body 里没出现的账号一律保留） | `merge_save_user_data()` |
| 全程锁 `_WRITE_LOCK` | 同上 |
| 原子写 `mkstemp` + `os.replace` | 同上 |
| 加载失败进 fatalBar，不拿空数据跑 | `app.js` `init()` |
| 保存失败 toast + 校验返回 | `app.js` `saveUserData()` |

外加：写盘前自动备份（3 分钟一份、留 200 份）、`/api/data_health` 体检接口、`restore_backup.py` 一键恢复。

**用户对这件事极其敏感**——他反复强调「备注、简介、历史记录是我一条条看出来的，千万别删」。**任何批量写数据前，先做逐字段 diff**（改前备份 → 改后对比 → 确认只有目标字段变化）。

---

## 6. 常用命令

```bash
cd /d D:\ai_Project\douyin-local-library\browser

# 全量重扫（约 1900 个文件夹约 20 分钟；重建 data.json，不动 user_data.json）
# ⚠️ /api/rescan 走的是增量（按文件夹 mtime），批量搬完文件后要扫两次才全
python src\scanner.py

# 扫完补 sec_uid（新下载的博主没有 xlsx，扫完 sec_uid 是空的）
python tools\fill_secuid_from_export.py --dry    # 先看能补几个
python tools\fill_secuid_from_export.py          # 从 export/*.csv 补（本地，3 秒）

# sec_uid 体检（联网，必须 3.12）
"C:\Python312\python.exe" tools\secuid_check.py --dry --by-secuid

# 删博主（四步，见第 8 节）
python tools\block_author.py "昵称" --delete

# 缓存
python tools\clean_cache.py --status
python tools\clean_cache.py --wipe

# 恢复标注
python tools\restore_backup.py --list
python tools\restore_backup.py --latest
```

---

## 7. 当前功能（一句话一个）

**浏览**：作者网格分页（无限滚动，监听 `.content`）· **「每作者一行」模式**（`#layoutBtn` 切换，一行作者表头 + 一行作品卡片，右侧常驻播放区点卡片就地播，不跳详情页）· 详情页左作品列表 / 右播放区 · 图文多图 · HEVC 原生硬解（无extension/解不动时自动转码兜底）· 竖屏「填满」裁切
**筛选**：搜索（全部/昵称/备注/简介/作品五种范围，作品搜索直接出作品卡并定位）· 排序 9 项 + 正反序 · 筛选面板（SEC_UID 有/无、星标、最后更新含自定义区间、**等级/状态/更新频率多选**、封面）· **新下载页另有一套 5 行筛选**（等级/标签/状态/频率/最后下载时间，每行行首圆钮 ＋包含 / －排除，**在服务端筛**所以翻页也一致）· **「全部作者」和「导出作者信息」两处面板的筛选结果会写成一句人话**（如「全部作品，去掉 纹身标签」），作者页那句在面板外面
**标注**：星标 · 标签 · 备注（note）· 别名（alias，显示时替代昵称）· 账号状态 8 类 · 作者等级 4 档 · 更新频率 4 档 · 历史记录 · 封面（视频截帧/图片原图）
**大小号**：多向关联组 · 整组同步等级（频率不跟组走，各记各的）· 自定义文件夹名 · 设为大号
**侧边栏**：功能（批量获取资料、获取最近收藏、导出作者信息、设置）/ 分类（全部作者、星标、大小号、黑名单、7 个状态）/ 等级（4 档）/ 更新频率（4 档）/ 标签 / 年份
**作品多选删除（移入回收站）**（详情页返回栏 `🗑`）：点一下进入选择模式，勾选不要的作品 → 「移入回收站」，文件搬到 `browser\recycle-bin\<批次>\`（保留原目录结构）、索引同步、写 `data/删除记录.jsonl`（含批次号）；在「文件管理 → 回收站」可「恢复」（搬回原位、作品插回索引）或「彻底删除」
**📁 文件管理**（侧边栏·功能）：文件夹改名（文件夹名跟着标注昵称走，改完同步索引不用重扫；结尾带点/空格的名字 Windows 存不了，会灰掉）/ 重复作品清理（每条作品两版并排成气泡：在用 / 隔离区副本，气泡下圆圈选「保留这份」，右上角「删除重复」执行；保留隔离区那份会覆盖回原位，索引不用重扫）/ **回收站**（一批一行，可恢复 / 彻底删除）
**获取最近收藏**：登录态抓「收藏 / 喜欢」→ 分「未下载过 / 下载过 / 黑名单」三类 · 显示「最新发布 vs 本地已存」相差天数 · 标该收藏作品是否已下载 · 一键加黑名单 · 可点进本地作者页再返回（滚动位置不丢）
**黑名单**：独立整页，可加（粘主页链接自动查 uid/昵称）/改原因（**标签制，可多选、可新建标签**）/移除；**在名单里的作者浏览端直接不显示**（磁盘文件保留）
**导出作者信息**：等级 / 标签 / 状态 / 更新频率**多选** → 预览 → CSV
**设置**：更新频率禁用状态（保存时把命中账号的频率**真清掉**）· 最新发布口径 · 数据库 / FFmpeg / 组件地址（生效值显示 + 修改）· 缓存清理；保存/取消按钮常驻页底
**批量获取资料**：后台跑，可关页面；三组设置（获取昵称/抖音号/简介、跳过封号与注销、六种顺序）；冲突进「待确认」由用户决定
**其他**：顶栏「⟳ 刷新」热加载（不重启服务）· 重新扫描真进度 · 抖音当前作品数 · SEC_UID 导出 · 状态说明栏（每个状态写一段自己的判定标准）

---

## 8. 已知问题 / 不建议动的

**A. 已知未修（不阻塞）**
1. 5 个账号缺 sec_uid（注销/封禁/私密，接口返回空），补不了。
2. 转码任务状态在内存（`TRANS_JOBS`），服务重启后前端轮询拿到 `missing`，重新点开该作品即重转（产物不受影响）。
3. `scanner.py` 的 `--incremental` 是死参数（解析了但没实现），要增量得自己做 mtime 比对。
4. **服务曾「跑着跑着就没了」，已修**：浏览器刷新/快速切页会让连接提前断，`socketserver` 默认把这堆 `ConnectionResetError` 的完整 traceback 打到 stderr，1 小时 40 分攒了 2.6MB 把输出管道写爆 → 进程 exit 1、端口只剩 `TIME_WAIT`。现在 `ReuseHTTPServer.handle_error()` 对 `ConnectionResetError/ConnectionAbortedError/BrokenPipeError/TimeoutError` 静默。**再遇到「页面打不开、HTTP 000」，先 `netstat -ano | grep :8723` 看有没有 LISTENING**，没有就重启；不要去翻业务代码。

**B. 不要做的事**
- 不要动 `cache/cover_cache/`（用户设的封面图，丢了补不回来）。
- 不要用昵称比对来判断「是不是同一个人」（改名很常见；只有 uid / sec_uid 稳定）。
- 不要为了「优化」把 `?v=` 去掉或改回长缓存。
- 不要把 `data.json` 塞进前端。

**C. 删博主必须四步**（少一步就删不干净）

> **2026-10-29 起简化**：`server.py` 已接入黑名单过滤——**只要作者进了 `黑名单_不下载.csv`，主页列表/搜索/统计就自动不再显示他**（磁盘文件原样保留）。所以「只想让人从主页消失、文件自己以后再删」时，**只加黑名单 + 重启服务即可**，不用走下面的 ③④。

```bash
python tools\block_author.py "昵称" --delete          # ①黑名单 ②删文件夹
curl -X POST http://127.0.0.1:8723/api/save -H "Content-Type: application/json" \
     -d '{"accounts":{"<uid>":null}}'                 # ③删标注
# ④从 data.json 删这个 key（原子写），或直接重扫
```
漏了 ③④，列表里还会显示这个账号，点开一片空白。（黑名单过滤上线后，即使漏了 ③④，主页也不再显示；但标注里残留的 uid 会在重扫时被「合成账号」逻辑过滤掉，不会回魂。）

---

## 9. 给新对话的开场话术（直接复制）

> 项目在 `D:\ai_Project\douyin-local-library\browser\`，是一个本地抖音下载内容浏览器（Python `ThreadingHTTPServer` + 原生前端，端口 8723，版本 `?v=20261103c124`）。
> 请先读 `docs\HANDOVER.md`（怎么接手）和 `docs\PROJECT.md`（技术细节与踩坑），再动手。
> 必须守住的：改前端要升 `?v=` 版本号；写 `user_data.json` 只能走 `merge_save_user_data()`；不要 fetch `data.json`；加设置项只改 `DEFAULT_SETTINGS`；本机 ffmpeg 无 libx264。
> 我的需求是：……
