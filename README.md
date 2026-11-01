# 抖音本地作品库 · douyin-local-library

> 把硬盘上**已经下载好**的抖音作品，变成可搜索、可筛选、可标注的本地网页。
> A local web UI for browsing, tagging and managing Douyin videos you already downloaded.
>
> **不是下载器，不是 SDK，也不联网抓数据** —— 一个 Python 本地服务（8723）+ 一个浏览器扩展的查询服务（8790），
> 无框架、无构建、无数据库服务，`python src/server.py` 就能跑。

给本地下载好的抖音作品套一层**可浏览、可标注**的界面。视频和图片早就落在硬盘上了，文件夹名全是 `UID<uid>_<昵称>_发布作品`，这个项目负责把这一堆文件夹变成能搜索、能筛选、能标记、能播放的本地网页。

两个部件：

| 部件 | 作用 | 端口 |
| --- | --- | --- |
| `browser/` | 本地网页应用（Python 标准库 `ThreadingHTTPServer` + 原生 HTML/CSS/JS） | 8723 |
| `douyin-author-marker/` | Chrome / Edge 扩展（MV3）+ 本地查询服务，在抖音网页里浮一张「本地存了多少作品」的小卡片 | 8790 |

技术栈刻意做得很轻：**无框架、无构建、无数据库、无 AI 依赖**，一个 `python src/server.py` 就能跑。

---

## 快速开始

```bat
start-all.bat
```

它一次拉起两个服务，最后回报是否都活着：

```
[1/2] 本地管理后台    http://127.0.0.1:8723
[2/2] 插件查询服务    http://127.0.0.1:8790
[OK] 8723 后台      ：运行中
[OK] 8790 查询服务  ：运行中
```

停止用 `stop-all.bat`。只想起其中一个，用各目录里自己的启动脚本。

**运行前必须改的东西**：`browser/src/paths.py` 第 56~66 行的外部路径（下载器目录、Python、FFmpeg）。全项目所有路径都从这一个文件出去，换机器只改这一段。

---

## 目录结构

```
douyin-local-library/
├─ start-all.bat / stop-all.bat     一次拉起 / 一次停掉两个服务
├─ browser/
│  ├─ src/                          全部服务端代码
│  │  ├─ server.py                  本地 HTTP 服务（8723），接口都在这里
│  │  ├─ scanner.py                 扫描下载目录，生成索引 data.json
│  │  ├─ watcher.py                 监听下载目录，新作品自动收录
│  │  ├─ download_db.py             下载记录小库（SQLite）
│  │  ├─ paths.py                   唯一的路径来源
│  │  └─ web/                       前端：index.html / app.js / style.css
│  ├─ tools/                        独立小脚本：清缓存、恢复备份、黑名单、补 SEC_UID
│  │  └─ legacy/                    历史脚本（已归档，不再维护）
│  ├─ docs/                         GUIDE / README / PROJECT / HANDOVER / archive
│  └─ data/ cache/ backup/ export/ recycle-bin/     运行时生成，不入库
└─ douyin-author-marker/
   ├─ extension/                    Chrome MV3 扩展源码
   └─ local-service/                bridge.py + 启停脚本
```

运行时才会生成的目录（`data/`、`cache/`、`backup/`、`export/`、`recycle-bin/`）不在仓库里，服务启动时会自动建。

---

## 数据说明

**这个仓库不包含任何账号数据。**

`browser/data/user_data.json` 是唯一不可再生的数据（星标、标签、账号状态、备注、大小号关系），只存在于本机，已在 `.gitignore` 里排除。`data.json` 是扫描产物，删了重扫即可。

安全起见以下内容一律不入库：`data/`、`backup/`、`export/`、`recycle-bin/`、`cache/`、浏览器配置目录，以及根下的 `_archive/`（个人过程记录）。

---

## 文档

| 想知道 | 看 |
| --- | --- |
| 某个功能怎么用 | [`browser/docs/GUIDE.md`](browser/docs/GUIDE.md) |
| 界面总览、快速上手 | [`browser/docs/README.md`](browser/docs/README.md) |
| 目录 / 接口 / 数据结构 / 踩坑 | [`browser/docs/PROJECT.md`](browser/docs/PROJECT.md) |
| 接手项目（硬约束、常用命令） | [`browser/docs/HANDOVER.md`](browser/docs/HANDOVER.md) |
| 插件怎么装 | [`douyin-author-marker/INSTALL.md`](douyin-author-marker/INSTALL.md) |

---

## 几个已知的坑（写在前面省得踩）

1. 改了 `web/` 里的 html/js/css，**必须同步升** `index.html` 里的 `?v=` 版本号，否则浏览器用旧缓存，看起来像「改了没反应」。
2. 写 `user_data.json` **只能走** `merge_save_user_data()`，直接 `open(..., "w")` 会整库覆盖。
3. 前端**永远不要** fetch `data.json`（几十 MB）。
4. 内部启动脚本里注释了为什么要清 `PYTHONPATH` / 设 `NO_PROXY`——本机启动器会注入代理和安全删除钩子，不处理会出现「每个请求 502」和「一删文件就崩」的怪现象。
