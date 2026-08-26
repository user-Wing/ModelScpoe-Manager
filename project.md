# ModelScope Manager 项目维护文档

> 本文件是 `ModelScpoe-Manager` 的低上下文维护契约。后续处理本项目时，优先阅读本文件，再按任务需要读取源码；不要默认遍历 `runtime` 的 Python 标准库或 Qt 二进制文件。

## 1. 项目定位

ModelScope Manager 是 Windows 便携式 ModelScope 仓库资源管理器：使用 PySide6 构建 GUI，使用官方 `modelscope-hub` SDK 访问模型/数据集仓库，使用本地 SQLite 保存账户、仓库缓存、文件索引、备份和图床记录，使用内置 `aria2-next` 执行可暂停/恢复的下载。

主要启动方式：

- 发布/便携运行：双击 `start.bat`，优先使用 `runtime\pythonw.exe`。
- 开发运行：`py -3.12 main.py`。
- 应用入口：`main.py` → `modelscope_manager.app.run()`。

## 2. 结构总览与运行链路

```text
start.bat / main.py
        ↓
modelscope_manager.app.MainWindow
        ├─ ModelScopeService / MultiAccountService ── ModelScope Hub SDK
        ├─ AccountStore / BackupStore / ImageStore ── data\manager.sqlite3
        ├─ FolderSizeIndex ───────────────────────── data\manager.sqlite3 或旧索引库
        ├─ Aria2DownloadRunner ───────────────────── runtime\tools\aria2-next.exe
        ├─ ModelScopeWebDAV ──────────────────────── AList/本地 WebDAV 客户端
        ├─ AuthenticatedMediaProxy ───────────────── 本机播放器的私有媒体转发
        └─ PotPlayer installer ───────────────────── embedded-tools\7zip-zstd\7z.exe
```

GUI 的耗时工作通过 `QThread` 子类执行，主线程只负责界面、状态和信号槽；上传、下载、备份、索引、图床上传和 PotPlayer 解压均有独立工作线程。

## 3. 按文件列表排序的功能说明

以下列表按项目文件名/路径排序。`runtime` 是随程序分发的运行时依赖，仅记录边界，不逐项维护。

### 3.1 根目录文件

| 文件 | 功能 |
|---|---|
| `.gitignore` | 忽略运行数据、缓存和 Python 字节码；本目录已初始化为本地 Git 仓库。 |
| `main.py` | 启动入口。优先把便携运行时的 site-packages 放入模块搜索路径，避免误载其他 Qt 绑定的同名 Fluent 包，再调用 `app.run()` 并返回退出码。 |
| `README.md` | 面向用户的功能、使用、部署和开发说明；产品行为变更时应同步检查。 |
| `start.bat` | Windows 便携启动脚本；切换到程序目录并优先调用内置 `runtime\pythonw.exe`。 |
| `THIRD_PARTY_NOTICES.md` | Python、PySide6、ModelScope SDK、aria2-next、7-Zip-zstd 等第三方许可和来源说明。 |
| `V1.0.2更新日志.md` | V1.0.2 面向用户的简要功能、修复与使用说明。 |
| `V1.0.3更新日志.md` | V1.0.3 面向用户的简要功能、修复与安全说明。 |
| `V1.0.4更新日志.md` | V1.0.4 下载队列与上传拖放修复说明。 |
| `project.md` | 本项目结构、约束、验证方式和更新日志；每次源码修改后追加摘要。 |

### 3.2 `modelscope_manager` Python 包（按文件名排序）

| 文件 | 代码功能与边界 |
|---|---|
| `__init__.py` | 包标识和版本号，目前为 `1.0.4`。 |
| `app.py` | 主应用编排层。创建 FluentWindow、导航页、Public/Token/网页登录账户仓库树、标签筛选、公开资源历史、可追加的上传队列、下载队列、备份、图床、播放器设置、托盘和 WebDAV 控制；定义后台线程。上传项保存独立目标路径，当前项可在 SDK 字节回调边界暂停、恢复或取消；网页登录仓库的删除、移动和重命名仍把上传阶段路由到 Token SDK。 |
| `backup.py` | 备份任务数据模型和 SQLite 存储。按文件大小/修改时间识别新增或变更文件，支持增量时间戳目录、同路径覆盖所需的记录、周期到期判断和云端同步时间记录；单文件达到 50 GiB 时列为跳过项。 |
| `database.py` | 主 SQLite 数据层。保存账户元数据及设备绑定加密 Token/网页登录会话、仓库缓存、远端条目、搜索索引、目录大小、备份任务/文件、图床记录和多标签映射；重新缓存仓库条目时会清理已消失路径的标签。 |
| `download_service.py` | aria2-next 下载适配层。定义下载规格和 `Aria2Tuning`，校验远端路径，生成 manifest，启动/连接 aria2 RPC，执行暂停/恢复/停止、动态下载限速、进度汇总和 SHA-256 校验；本地文件保留断点与已下载内容。 |
| `folder_index.py` | 递归远端条目的目录大小计算与持久化缓存。把每个文件累加到根目录及所有祖先目录，供 GUI 和 WebDAV 复用；GUI 仅显示上次缓存值，缺少缓存时显示 `--`，索引任务完成后刷新为最新值。 |
| `fluent_ui.py` | PySide6-Fluent-Widgets UI 薄封装：任意右侧控件设置卡片、复杂设置面板卡片、QCheckBox 兼容开关和无原生阴影且按屏幕边界定位的下拉框。 |
| `image_bed.py` | 图床记录与本地缓存。记录账户、仓库、远端路径、直链和缓存文件；图片上传成功后由 GUI 写入记录并可复制直链/打开缓存。 |
| `locales/en_US.json` | English UI 文本。 |
| `locales/zh_CN.json` | 简体中文 UI 文本。 |
| `localization.py` | `LocaleManager`：读取语言 JSON，提供缺失时回退原文的文本查找。 |
| `media_proxy.py` | 私有媒体的本机 HTTP Range 代理。仅监听 `127.0.0.1`，内存保存短期路由，向 ModelScope 请求时注入 Token，转发 Range/媒体响应；跨主机重定向时移除 Authorization/Cookie，避免泄露凭据。 |
| `player_installer.py` | PotPlayer 归档安全安装。固定归档大小和 SHA-256，使用内置 7z-zstd 解压到 staging，验证可执行文件后事务式替换旧目录并失败回滚。 |
| `public_pools.py` | 公共资源池历史。以原子替换方式保存资源搜索成功加载过的公开仓库，支持单项移除和清空，重启后恢复 WebDAV `public` 挂载。 |
| `security.py` | Windows DPAPI 的 ctypes 封装，提供 `protect`/`unprotect`；只处理 Token 字符串，不在日志中输出凭据。 |
| `service.py` | ModelScope SDK 适配边界。定义 `Repository`、`RemoteEntry`，解析官方公开链接，规范化远端路径，列出账户仓库和文件，获取直链，上传文件/文件夹并桥接进度；`ModelScopeWebService` 只复用网页登录会话做读取/下载并显式拒绝上传，`MultiAccountService` 按仓库路由到已验证账户。数据集文件必须优先使用 SDK 的 `list_dataset_files_paginated`，仓库 API `page_size` 上限为 50。 |
| `startup.py` | Windows 当前用户开机自启注册表读写，生成指向便携目录 `main.py` 的 `pythonw.exe` 启动命令。 |
| `storage.py` | 便携配置与设备身份。使用程序目录 `data` 下的 `settings.ini`、`device.id` 等文件；设备标识变化时清除无法解开的旧 Token，保存 Token 时与设备标识绑定。 |
| `styles.py` | 浅色/深色 PySide6 QSS，包括侧栏、卡片、树/表格、拖放区、按钮、进度条、`QTimeEdit` 和紧凑视图基础样式。 |
| `transfer_policy.py` | 上传/下载共享限速策略。支持默认速率、每日分时规则、跨午夜时间段和重叠规则最后一条覆盖；`SharedRateLimiter` 为上传 SDK 流和其他共享上传任务提供进程级聚合限速。 |
| `web_session.py` | ModelScope 网页登录最小会话模型、当前网页用户名解析，以及模型/数据集单文件删除请求；请求不记录 Cookie 或 CSRF 值。 |
| `webdav_server.py` | 只使用 Python 标准库实现的 WebDAV 网关。虚拟挂载 `models`、`datasets`、`public`，支持 Basic Auth、目录列举、远程读取和上传；公共池只读，ModelScope 当前不支持的删除/移动/复制操作会拒绝。目录属性使用 `FolderSizeIndex`。 |
| `assets/check.svg` | QSS 勾选框图标资源。 |

### 3.3 `tests` 测试文件（按文件名排序）

| 文件 | 覆盖范围 |
|---|---|
| `test_app_helpers.py` | GUI 辅助格式化、路径和 WebDAV 端口探测。 |
| `test_backup.py` | 备份任务保存、变更扫描、上传记录等。 |
| `test_database.py` | 数据库初始化、账户和索引搜索、多账户路由。 |
| `test_download_service.py` | 下载规格、路径安全、调优参数和校验行为。 |
| `test_folder_index.py` | 目录聚合大小、持久化索引及“无缓存”与“零字节文件夹”区分。 |
| `test_fluent_ui.py` | FluentWindow 与自定义 Fluent 控件的继承、任意控件卡片和开关信号兼容。 |
| `test_image_bed.py` | 图床记录和缓存生命周期。 |
| `test_localization.py` | 语言加载和回退。 |
| `test_media_proxy.py` | 私有媒体代理、Range 和凭据转发/重定向安全。 |
| `test_player_installer.py` | 归档校验、解压安装和失败回滚。 |
| `test_public_pools.py` | 公共仓库历史的读写、去重、单项移除和清空。 |
| `test_service.py` | ModelScope URL/路径、仓库条目和上传辅助逻辑。 |
| `test_startup.py` | Windows 启动命令生成。 |
| `test_storage.py` | 设备身份和配置 Token 恢复。 |
| `test_transfer_policy.py` | 限速、跨午夜和重叠规则。 |
| `test_upload_thread.py` | 上传项独立目标路径和取消前的安全停止。 |
| `test_styles.py` | 浅色/深色主题及 `QTimeEdit` 样式覆盖。 |
| `test_webdav_server.py` | WebDAV 虚拟目录、鉴权、读取和上传行为。 |
| `upload_payload/` | 上传测试用的嵌套目录和独立文件，不是生产数据。 |

### 3.4 部署目录与数据文件

| 路径 | 作用与维护规则 |
|---|---|
| `embedded-tools/7zip-zstd/` | PotPlayer 等归档的解压工具及许可证；不要替换而不重新验证版本和许可证。 |
| `runtime/` | 内置 Python 3.12、PySide6、PySide6-Fluent-Widgets、无边框窗口依赖、ModelScope Hub SDK、requests、tqdm、aria2 工具等便携运行时；通常只在发布/依赖问题中检查。 |
| `data/settings.ini` | 用户普通配置，可能包含语言、路径、限速、播放器、WebDAV 等设置。 |
| `data/device.id` | DPAPI 设备绑定标识。 |
| `data/manager.sqlite3` | 主数据库，包含账户、加密 Token、仓库/文件索引、备份和图床记录。 |
| `data/folder_sizes.sqlite3` | 旧版目录大小数据库；初始化时可迁移到主数据库，后续不要假设它仍是唯一来源。 |
| `data/public_pools.json` | 公共资源池持久化历史。 |

## 4. 关键约束与风险

- ModelScope 仓库类型目前只支持 `model` 和 `dataset`；远端路径统一用 `/`，禁止 `..` 路径穿越。
- 仓库列表请求的 `page_size` 使用 50；数据集文件列表不能依赖可能截断的通用接口，必须保留分页调用。
- 单个上传文件超过 50 GiB 跳过；备份逻辑对达到或超过 50 GiB 的文件跳过，修改阈值时必须同步用户提示和测试。
- Token 必须经过 DPAPI 且绑定设备；禁止把 Token 写入日志、播放器命令行或跨主机重定向请求。
- 私有媒体代理只监听回环地址，不缓存媒体；Range、响应头和跨主机重定向行为属于安全边界。
- 上传限速是进程级聚合限制，不能退化为每文件限速；下载限速通过 aria2 全局设置动态更新。
- ModelScope Token API 当前不提供可靠删除能力；程序内删除使用独立 DPAPI 设备绑定的网页登录会话。所有上传和秒传提交仍必须走有目标仓库权限的 Token SDK，禁止回退到网页上传；WebDAV 删除、移动、复制继续保持拒绝。
- PotPlayer 安装必须先做固定大小和 SHA-256 校验，采用 staging/backup 回滚；成功后才删除归档。
- `runtime`、`data`、归档和缓存属于部署/运行状态，不应在普通源码修改中批量清理或覆盖。
- 本目录已完成 `git init`。执行环境与目录所有者不同，自动化检查 Git 状态时使用仅本命令生效的 `-c safe.directory=<本目录>`，不要因此改写用户的全局 Git 配置。

## 5. 推荐验证方式

在项目根目录执行：

```powershell
runtime\python.exe -s -m unittest discover -s tests -p "test_*.py"
runtime\python.exe -s -m compileall -q main.py modelscope_manager tests
```

涉及 GUI、WebDAV、aria2、播放器或便携部署的修改，还要按影响范围做可见行为验证：确认 `start.bat` 可启动、设置可保存、上传/下载队列状态正确、WebDAV 端口和鉴权正确、私有媒体不泄露 Token、安装失败能回滚。单纯通过语法检查或单元测试不能证明完整便携发布链路可用。

## 6. 后续修改记录规则

每次修改源码、测试、部署脚本或用户可见行为后，在本文“更新日志”最前面追加一条：日期、修改文件、行为摘要、验证结果和剩余风险。若只做分析或未改文件，不追加虚假记录。修改 `README.md` 或依赖版本时，也应说明是否同步检查本文件中的约束。

## 7. 更新日志

### 2026-08-25 — V1.0.4 下载显示与上传队列修复

- `modelscope_manager/app.py`：下载表格为“本地位置”保留可见空间，下载运行阶段最多显示 99%，校验完成后再显示 100%；下载状态回调按 Windows 规范化路径定位表格行。上传期间拖放追加任务时，优先使用已锁定的上传会话，并按仓库类型和 ID 判断是否仍为同一仓库，不再受仓库更新时间等刷新元数据影响。
- `modelscope_manager/__init__.py`、`modelscope_manager/app.py`、`README.md`：版本更新为 `1.0.4`；`V1.0.4更新日志.md` 同步面向用户的修复说明。
- `tests/test_app_helpers.py`：覆盖仓库稳定身份、Windows 本地路径回调匹配，以及运行中进度不提前显示 100%。
- 验证：相关 34 项及全量 115 项单元测试通过；`runtime\python.exe -s -m compileall -q main.py modelscope_manager tests`、版本导入冒烟和 `git diff --check` 通过。
- 风险：未执行真实 ModelScope 上传或远端 aria2 下载，避免未经请求改动云端仓库；真实传输界面仍需进行一次人工可见行为确认。

### 2026-08-25 — Repair 副本便携启动与风险修复回归验收

- `runtime/`：从原始 `ModelScpoe-Manager` 的同版本便携运行时仅补回 Repair 副本缺失的 4037 个文件（约 209.87 MiB），不覆盖 Repair 已有文件；恢复 `unittest`、`pywin32_bootstrap`、Qt 资源及 `runtime/tools` 等启动和测试依赖。未修改或清理 `data/`、Token、Cookie、数据库和缓存。
- `modelscope_manager/web_session.py`：网页登录请求继续使用绑定 `.modelscope.cn` 的 CookieJar，保留跨重定向 Cookie 域限制，同时恢复对既有 `requests.get/delete/post` 测试替身和调用边界的兼容。
- `tests/test_app_helpers.py`、`tests/test_database.py`、`tests/test_download_service.py`、`tests/test_media_proxy.py`、`tests/test_web_session.py`：把旧断言更新为风险修复后的行为，覆盖未知 visibility fail-closed、严格 DPAPI 的隔离测试、aria2 JSON-RPC 内存注入凭据、非官方域不主动发送 Token，以及网页登录 Cookie 域。
- `tests/test_http_security.py`、`tests/test_local_paths.py`：新增 ModelScope 官方域识别、仿冒域拒绝、跨 origin 重定向移除认证头并保留 Range，以及上传/备份链接类路径拒绝测试。
- 验证：`runtime\python.exe -s -m unittest discover -s tests -p "test_*.py"` 全量 112 项通过；`runtime\python.exe -s -m compileall -q main.py modelscope_manager tests` 和 `git diff --check` 通过。离屏主入口稳定进入 Qt 事件循环；按 `start.bat` 的生产链路运行 `runtime\pythonw.exe main.py` 后保留 `ModelScope Manager` 可见窗口，用户完成人工确认并关闭窗口。
- 风险：本轮没有执行真实 ModelScope 登录/删除、aria2 远端下载、AList WebDAV PUT 或远端备份恢复，以避免未经请求地读写云端资源；全量测试仍会输出既有的临时数据库异步搜索 `no such table: entries` 清理竞态堆栈，但最终结果为 `OK`。

### 2026-08-25 — V1.0.3 源码发布准备

- `modelscope_manager/__init__.py`、`modelscope_manager/app.py`、`README.md`：版本更新为 `1.0.3`，状态栏与源码说明同步显示新版本。
- `V1.0.3更新日志.md`、`project.md`：汇总多网页登录账户、程序内批量删除、移动与区分大小写重命名、索引增量更新及本轮 Fluent UI 修复，并明确网页登录凭据与 Token SDK 的安全边界。
- 发布边界：GitHub 仅提交源码、测试与文档；现有 `.gitignore` 继续排除 `data/`、`runtime/`、`dist/`、Python 字节码和上传缓存，不清除或上传本机已保存的 Token、Cookie、数据库及其他账户数据。
- 验证：全量 108 项单元测试通过，`runtime\python.exe -s -m compileall -q main.py modelscope_manager tests`、版本导入冒烟与 `git diff --check` 通过。
- 风险：本次只发布源码，不创建 GitHub Release 或便携压缩包；全量测试仍会输出既有的临时数据库异步搜索 `no such table: entries` 清理竞态堆栈，但测试最终结果为 `OK`。

### 2026-08-25 — 深色传输状态与 Fluent 设置布局修复

- `modelscope_manager/app.py`、`modelscope_manager/styles.py`：深色模式的上传进度条文字固定为高对比白色，上传队列中等待、上传中和暂停百分比显式使用当前主题文本色，主题切换后立即重绘队列。Token 登录与第三方播放器的 `+/-` 移到对应标题同行并顶部对齐；账户连接状态与在线登录分割线之间增加 12px 留白；“下载与传输”及 aria2 详细配置的纵向间距统一增至 14px。
- `modelscope_manager/fluent_ui.py`：设置页 `CleanComboBox` 保留透明圆角和无原生阴影，但改用无动画菜单并在关闭时请求所属窗口重绘，避免透明顶层展开动画留下旧帧残影。
- `tests/test_app_helpers.py`、`tests/test_fluent_ui.py`、`tests/test_styles.py`：覆盖标题按钮几何对齐、分割线留白、下载面板间距、深色上传状态前景色、无动画下拉菜单和深色进度文字。
- 验证：相关 31 项及全量 108 项单元测试通过，`compileall` 与 `git diff --check` 通过；深色模式真实启动程序，逐项展开账号、下载与传输、媒体播放器设置，并连续打开/关闭主题下拉菜单，未观察到残影，布局与间距符合预期。程序验证后保持打开，未清除 Token、Cookie 或账户数据。
- 风险：下拉设置取消展开动画以优先保证稳定重绘，交互会立即显示而不再具有下拉位移动画；其他非设置页原生 `QComboBox` 不使用该封装，行为保持不变。

### 2026-08-25 — 文件夹批量删除与索引增量更新

- `modelscope_manager/web_session.py`、`modelscope_manager/app.py`：文件夹删除从逐文件独立请求改为每批最多 100 条 `delete` action 的单次提交。批量请求异常时使用带 `Root` 和分页参数的目标目录查询识别服务端已实际删除的文件，仅对仍存在路径回退单文件删除，避免响应超时造成“网页已消失、软件仍报失败”的误判。
- `modelscope_manager/database.py`、`modelscope_manager/folder_index.py`：新增按路径前缀删除条目/标签的 SQLite 事务，以及按已删文件大小递减祖先目录、移除目录子树的大小索引更新；删除完成后的界面刷新不再调用全仓 `cache_entries()` 或重写全部 `folder_sizes`。
- `tests/test_web_session.py`、`tests/test_app_helpers.py`、`tests/test_database.py`、`tests/test_folder_index.py`、`README.md`：覆盖批量提交、目录分页查询、不确定提交对账、相似路径隔离、标签清理和目录大小增量更新，并同步删除行为说明。
- 验证：相关 45 项及全量 107 项单元测试通过，`compileall` 与 `git diff --check` 通过。真实远端两轮均在 `ARXChem/Animations-List/test` 下由 Token SDK 上传 3 个小文件，再由网页登录会话单次批量删除；第二轮使用生产 `DeleteThread` 删除 `test/manager-delete-fa582de0`，结果为 3 个成功、0 个失败，目录级查询剩余 0 个。
- 风险：超过 100 个文件的目录仍需多次批量提交，后续批次失败时前面已完成的提交不可回滚；若批量请求和随后的目录查询同时发生网络故障，会回退逐文件请求并保留真实失败项。全量测试中既有的临时数据库搜索回调 `no such table: entries` 清理竞态仍可能输出，但不影响测试最终结果。

### 2026-08-25 — 多网页登录账户与程序内删除、移动、重命名

- `modelscope_manager/database.py`、`modelscope_manager/app.py`：新增独立 `web_accounts` 多账户表和设置页表格，支持 `+` 添加、账户名称编辑、逐行在线登录与“成功”状态。旧版附着于 Token 账户的加密会话会原样登记为独立网页登录账户；Token 账户移除不再连带删除该网页登录会话，Cookie/Token 数据未在本次更新中清空。
- `modelscope_manager/app.py`：仓库树分成 Public、Token 登录账户和网页登录账户三段。资源菜单统一显示删除、移动、`重命名（区分大小写）`；Public 和 Token 下对应动作置灰并提供只读/改用在线登录提示。删除文件夹时逐个删除其全部文件，成功后只更新本地仓库条目、文件夹大小和当前视图，不重新请求整个远端树。
- `modelscope_manager/service.py`、`modelscope_manager/web_session.py`：网页登录会话可驱动账户校验、仓库列举、分页读取和安全下载；网页服务的上传方法显式拒绝。复制、移动、重命名和普通上传都按目标仓库重新选择 Token SDK 服务；移动/重命名先经过复制阈值判断，下载到临时目录，只有全部目标上传成功后才调用网页删除旧路径。
- `tests/test_app_helpers.py`、`tests/test_database.py`、`tests/test_service.py`、`tests/test_web_session.py`、`README.md`：覆盖旧会话迁移、网页登录账户独立生命周期、同主机 CSRF 注入与第三方主机隔离、网页上传拒绝、递归路径映射、上传失败不删除源文件和模型/数据集删除端点，并同步用户说明。
- 验证：全量单元测试通过（102 tests），`compileall` 与 `git diff --check` 通过。真实远端在 `ARXChem/Animations-List/Violet Evergarden` 选取 128 字节的 `Disc/Soundtracks/LACA-9751~3/CD/Scan/!CREDIT.txt`：Token SDK 上传后网页会话删除旧路径，依次完成移动到临时目录、区分大小写重命名为 `CASE_TEST_!CREDIT.txt`、再移动回原路径；三次均为上传零失败后删除一个源文件，最终原路径存在、临时前缀为空、目标前缀普通文件仍为 9 个。
- 风险：网页登录会话仍可能由服务端过期或撤销；没有能访问目标仓库的 Token 账户时，上传、复制目标写入、移动和重命名会停止并提示，不会尝试网页上传。多文件删除在服务端逐项提交，若网络中途失败，已删除项不可回滚，界面会保留失败项并报告成功/失败数。全量测试仍会输出既有的临时数据库搜索回调 `no such table: entries`，测试结果为 `OK`。

### 2026-08-25 — ModelScope 在线登录会话捕获与删除链路验证

- `modelscope_manager/app.py`、`modelscope_manager/web_session.py`：账号设置区分“Token 登录”和“ModelScope 账户 在线登录”；内置 Qt WebEngine 使用非持久化配置打开官方登录页，只捕获 `m_session_id`、`csrf_session`、`csrf_token`，校验网页登录后交给账号存储。修复 Qt `QNetworkCookie.domain()` 返回 `str` 而名称和值返回 `QByteArray` 时会话捕获静默失败的问题。
- `modelscope_manager/database.py`：新增账户网页登录会话表；会话 JSON 使用 Windows DPAPI 加密并绑定当前设备，移除账户或设备身份失效时同步销毁。
- `tests/test_app_helpers.py`、`tests/test_database.py`、`tests/test_web_session.py`、`README.md`：覆盖真实 Qt Cookie 类型、会话加密/设备绑定/清理、CSRF 请求头、网页登录校验和删除请求，并同步说明在线登录的安全边界。
- 验证：全量单元测试通过（95 tests），`compileall` 通过；真实登录信息成功保存并重载，使用网页会话删除 `ARXChem/Animations-List` 中 124 字节的 `Violet Evergarden/Disc/Scans/The Movie/Theater Goods/Postcard from Ecartehiga/Text.txt`，接口返回 `Code=200`，SDK 重新分页列举确认该路径消失、目标前缀普通文件由 10 个变为 9 个。测试过程中未输出 Token、Cookie 或 CSRF 值。
- 风险：网页登录会话可能被 ModelScope 服务端过期或撤销，需要用户重新在线登录；Qt WebEngine 增加便携运行时体积。当前完成的是安全会话捕获、保存和底层删除能力，资源菜单中的程序内删除入口仍需单独接入确认交互；WebDAV 的删除/移动/复制继续保持拒绝。

### 2026-08-24 — V1.0.2 便携构建与发布准备

- `modelscope_manager/__init__.py`、`modelscope_manager/app.py`、`README.md`：版本更新为 `1.0.2`，状态栏同步显示新版本；说明 GitHub 源码仓库不提交约 220 MB 的便携 Python 运行时和本地 `data` 数据，完整构建包仍包含运行时。
- `modelscope_manager/app.py`、`modelscope_manager/fluent_ui.py`：视频缩略图从首帧改为在打开输入前快速定位至 1.5 秒，尽量利用 HTTP Range 减少远端读取；缓存源图提高到 320×180、JPEG 质量设为 3，并通过新缓存键淘汰旧黑屏缩略图。复杂设置首行固定为 70px 垂直居中，展开/收起使用 180ms 三次缓动并逐帧驱动父设置组重排，避免动画中内容被裁切。
- `tests/test_app_helpers.py`、`tests/test_fluent_ui.py`：增加视频快速定位、较高分辨率、JPEG 参数、完整 `SettingCardGroup` 展开/收起高度和动画状态回归测试。
- `V1.0.2更新日志.md`、`.gitignore`：基于本文件整理简要用户更新日志；源码提交排除 `runtime/`、`data/` 和 `dist/`，保留源码、测试、文档及便携工具许可文件。
- 验证：`runtime\python.exe -s -m unittest discover -s tests -p "test_*.py"` 通过（90 tests），`runtime\python.exe -s -m compileall -q main.py modelscope_manager tests` 通过。使用 7-Zip-zstd 构建 `dist\ModelScope-Manager-V1.0.2-Windows-x64.zip`（84,313,278 bytes，SHA-256 `008B15889D094D6E2AC231C1DE6CF7F283BF644C672591D002DAB7DBFB2122FD`），排除 Git、运行数据、测试和维护文件后压缩包完整性测试通过；重新解压后版本模块返回 `1.0.2`，真实 Windows 窗口状态栏显示 `ModelScope Manager 1.0.2`，设置首行居中、账号卡片展开重排及深色模式加减图标可见。
- 风险：远端视频服务不支持 Range 时，FFmpeg 仍可能从文件开头读取到目标时间；高并发缩略图仍会提高 CPU、网络和磁盘峰值。全量测试退出前仍会输出既有的临时数据库搜索回调 `no such table: entries` 与 Qt 对象回收警告，但测试结果为 `OK`，本次未扩展范围修改该测试清理时序。

### 2026-08-24 — 设置页下拉与对齐、激进并发、图床记忆和高级搜索修复

- `modelscope_manager/fluent_ui.py`、`modelscope_manager/app.py`、`modelscope_manager/styles.py`：Fluent 下拉菜单恢复透明顶层圆角，同时保留无原生投影标志，消除浅色模式黑色矩形和深色黑影；基本设置与个性化右侧控件预留 40px 尾部空间。账号、下载、播放、索引和 WebDAV 复杂卡片改为标准 70px 图标/标题/说明/展开箭头首行，详细面板按需展开；添加/移除账户和添加/移除播放器改用可随主题着色的 Fluent 图标按钮。
- `modelscope_manager/app.py`：缩略图处理从大仓库每批 4 项、2 线程、900ms 间隔提高到每批 96 项、最多 32 线程、10ms 间隔，小仓库提高到每批 48 项、最多 16 线程；空闲等待从 750ms 降为 150ms，搜索防抖从 220ms 降为 80ms。该策略会显著提高 CPU、网络、磁盘和 FFmpeg 峰值，以更快填充缩略图与搜索反馈。
- `modelscope_manager/app.py`：图床仓库按账户分别持久化，并避开 Qt 对 tuple `userData` 的 `findData()` 错误首项命中，重建仓库选项后精确恢复上次选择。
- `modelscope_manager/database.py`、`modelscope_manager/app.py`：本地索引搜索移除 500 项显示上限，全部类型返回所有已索引文件；与公开资源页统一使用 Everything 风格多词 AND、字段、引号和通配符匹配。名称、类型、真实大小、仓库与路径继续使用 Python 层稳定排序。
- `tests/test_database.py`、`tests/test_app_helpers.py`、`tests/test_fluent_ui.py`、`README.md`：增加 621 项无截断搜索、多片段/字段匹配、激进并发参数、组合框透明菜单、折叠卡片、右侧留白和图床仓库恢复覆盖，并同步用户说明。
- 验证：`runtime\python.exe -s -m unittest discover -s tests -p "test_*.py"` 通过（88 tests）；`runtime\python.exe -s -m compileall -q main.py modelscope_manager tests` 与语言 JSON 解析通过。真实 Windows 主入口中检查深色及浅色下拉，均无黑色矩形；设置控件尾部留白、复杂卡片首行对齐/展开箭头和深色模式 Fluent 加减图标均可见，检查后恢复原深色主题。
- 风险：高并发缩略图会显著增加 CPU、带宽、磁盘读取与同时运行的 FFmpeg 任务；仍可通过“缩略图生成线程数”手动降低。搜索完整性由本地索引保证，远端仓库若尚未完成分页索引，仍需先读取或更新索引。

### 2026-08-24 — 设置选项值、关闭询问与 WebDAV 端口误报修复

- `modelscope_manager/app.py`：修正 Fluent 下拉框的 `addItem` 参数，明确通过 `userData` 保存语言、关闭行为、主题、复制阈值单位、WebDAV 协议/监听范围和 aria2 策略值；此前第二个位置参数被 Fluent 组件解释为图标，导致 `currentData()` 为 `None`，进而写入 `theme=@Invalid()`、`language=None`、`alist/host=@Invalid()`。启动时会把已损坏的选择项恢复为安全默认值并写回配置，关闭行为默认恢复为询问，WebDAV 监听恢复为 `127.0.0.1`。
- `tests/test_app_helpers.py`：用损坏的便携设置构造主窗口，覆盖选择项自愈、7 个设置分组存在、关闭询问取消路径，以及语言、主题、字号、图形、滚轮保护、索引/预览、复制、aria2、限速、播放器和 WebDAV 设置的即时持久化。
- 验证：`runtime\python.exe -s -m unittest discover -s tests -p "test_*.py"` 通过（85 tests）；`runtime\python.exe -s -m compileall -q main.py modelscope_manager tests` 与两份语言 JSON 解析通过；主入口启动后 `http://127.0.0.1:9864/` 返回预期的 `401` 和 `Basic realm="ModelScope Manager"`，停止验证进程后同一端口恢复可用。项目测试命令统一增加 `-s`，避免用户级 PyQt5 版 `qfluentwidgets` 抢先于内置 PySide6 依赖加载。
- 风险：主窗口和设置交互已由隔离运行时构造测试覆盖；当前自动化桌面未暴露测试进程窗口，尚未通过真实鼠标逐项观察下拉弹层和关闭对话框的最终视觉位置。

### 2026-08-24 — FluentWindow 残影、重影与 WebDAV 端口可见性修复

- `modelscope_manager/app.py`、`modelscope_manager/styles.py`、`modelscope_manager/windows_effects.py`：删除 FluentWindow 自带 Mica 之外的第二套自定义 DWM 背景实现，不再给 Qt 顶层窗口设置 `WA_TranslucentBackground`；六个动画栈页面改为不透明背景，页面切换由 300ms 缩短为 160ms。Mica 仅由 FluentWindow/Windows 合成器单层处理，不支持时回退不透明背景，避免软件 OpenGL、透明顶层和双重 DWM 合成造成滚动残影、旧帧重影和高重绘开销。
- `modelscope_manager/app.py`：修复个性化旧容器释放后 `graphics_status` 仍被槽函数访问的问题，将状态标签纳入正式 Fluent 卡片生命周期。WebDAV 端口从复杂面板内部提升为独立 `ControlSettingCard`，端口冲突自动切换后的实际值无需展开内部布局即可看到；下方监听范围、用户名、密码、URL 和启停功能保持不变。
- `tests/test_app_helpers.py`、`tests/test_styles.py`、`README.md`：增加顶层不透明、全部页面不透明、端口控件归属和端口持久值回归断言，更新 Mica 与端口入口说明。
- 验证：`runtime\python.exe -s -m unittest discover -s tests -p "test_*.py"` 通过（85 tests）；`runtime\python.exe -s -m compileall -q main.py modelscope_manager tests` 通过。真实 Windows 前台使用当前配置连续切换资源页/设置页并长距离滚动，截图未见旧页面残留或重复绘制；WebDAV 独立端口卡片显示当前保存值 `9868`，下方完整配置可见。
- 风险：自动截图确认了静态重绘结果，但不同显卡驱动、刷新率和远程桌面环境下的逐帧流畅度仍可能有差异；若关闭 GPU 加速，Qt 主界面仍使用栅格绘制，但不再叠加透明顶层窗口。

### 2026-08-24 — PySide6-Fluent-Widgets / Windows 11 设置风格 UI 重构

- `modelscope_manager/app.py`、`modelscope_manager/fluent_ui.py`、`modelscope_manager/styles.py`：主窗口迁移为 `FluentWindow`，现有六个业务页面通过 `addSubInterface()` 注册到内置动画栈；左侧导航使用 Fluent 选中指示条和无边框标题栏，设置入口固定在底部，内容区新增底部状态栏。设置页使用透明 `ScrollArea`、7 个 `SettingCardGroup`、`ControlSettingCard`、Fluent 开关/数字框与统一 28px/24px/18px 留白；复杂账户、aria2、播放器、WebDAV 和索引控件只更换卡片容器，原信号槽及业务逻辑不变。
- `modelscope_manager/app.py`、`modelscope_manager/fluent_ui.py`：主题切换统一调用 Fluent 浅色/深色主题和 `#0078D4` 蓝色强调色；新增 9–18pt 全局字号并即时同步 Qt、Fluent 和已加载 Matplotlib。`CleanComboBox` 移除原生下拉阴影窗口并按当前屏幕可用区域约束高 DPI 弹出位置；启动前启用高 DPI PassThrough 并设置微软雅黑 UI 字体。设置仍使用便携 `QSettings` 即时持久化。
- `main.py`、`runtime/Lib/site-packages`、`THIRD_PARTY_NOTICES.md`、`README.md`：便携运行时加入 PySide6-Fluent-Widgets 1.11.3、PySideSix-Frameless-Window 0.8.2、darkdetect 0.8.0 和 pywin32 312；开发启动也优先加载该便携依赖，避免误载 PyQt5/PyQt6 的同名 `qfluentwidgets`。记录 GPLv3/商业授权边界及依赖许可，并同步新的导航、设置分组、主题和字号行为。
- `tests/test_fluent_ui.py`：增加 Fluent 继承、任意控件卡片和开关信号兼容测试。验证：隔离用户 site-packages 的 `runtime\python.exe -s -m unittest discover -s tests -p "test_*.py"` 通过（85 tests）；`runtime\python.exe -s -m compileall -q main.py modelscope_manager tests` 通过；临时空白设置下最小平台渲染确认设置卡片无横向截断且滚动布局可构造。
- 风险：最小平台不提供真实 Windows 字体栅格、DWM 亚克力和前台高 DPI 多屏环境，因此最终无边框拖动、系统 Snap、中文字体观感、下拉框跨屏位置及动画流畅度仍需在真实 Windows 前台手动确认。PySide6-Fluent-Widgets 的 GPLv3 仅适合相容的非商业发行；商业发行必须另购授权。

### 2026-08-24 — 拖放上传、图床公有直链与 Ctrl+V 图片上传修复

- `modelscope_manager/app.py`：拖放组件的内部标签改为不拦截鼠标/拖放命中，并持续接受合法的本地文件拖动；普通上传区与图床上传区共用该修复。图床页新增页面级 `Ctrl+V`，支持资源管理器复制的图片文件和剪贴板位图；位图会暂存为 PNG，并在上传完成、失败或线程中断后清理。
- `modelscope_manager/app.py`：图片在进入上传线程前同时检查支持的扩展名和 Qt 实际可读性，扩展名伪装的非图片会被拒绝。图床上传完成后按仓库可见性构造直链：公开仓库优先保存 `/resolve/master/` 公有地址，私有仓库继续保存不含 Token 的 API 地址。
- `tests/test_image_bed.py`、`README.md`：增加图片内容校验、拖放事件命中、公有图床直链和剪贴板临时文件清理覆盖；同步图床拖放、粘贴及直链说明。
- 验证：`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（75 tests）；`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过。离屏 Qt 事件验证确认拖放区接受本地文件，文本伪装 `.png` 被拒绝、真实 PNG 可读。
- 风险：未向真实远端仓库写入测试图片，以避免产生未经请求的云端文件；Windows 前台从资源管理器拖放和第三方剪贴板软件的特殊 MIME 组合仍依赖 Qt 的系统事件转换。

### 2026-08-24 — 搜索完整性回归与公开直链可见性修正

- `modelscope_manager/app.py`：统一识别 ModelScope 仓库可见性的文本值和数值枚举（`5` 为公开，`1`/`3` 为私有/内部）；账户下的公开仓库复制文件直链时使用不含 Token 的 `/resolve/master/` 公开地址，私有仓库使用 `/api/v1/.../repo?Revision=master&FilePath=...` 并显示 API 形式提示。未知可见性且当前持有 Token 时按私有处理，避免误发公开链接；外部播放和跨仓库复制复用同一隐私判断。
- `modelscope_manager/app.py`：搜索排序下拉槽显式接收 Qt 的索引参数，确保名称、类型、真实大小和路径选项均能触发重排；保留完整分页清单上的 Everything 风格多关键词 AND 搜索，可跨完整相对路径匹配。
- `tests/test_app_helpers.py`、`tests/test_service.py`：增加 ModelScope 数值可见性、公开/私有 URL 形式及分页结果不会退化为点号/数字首批的回归覆盖。
- 验证：应用自身服务链只读加载公开大型数据集 `ARXChem/Animations-List` 得到 2303 项，确认包含 `README.md`、`images/` 和第 24 页末项；离屏 GUI 验证大小升降序与 `z mp4` 跨路径分词搜索；离屏剪贴板验证公开图片和私有 `README.md` 分别生成指定两种 URL。全量单元测试与 `compileall` 通过。
- 风险：大型公开仓库的读取耗时仍取决于 ModelScope 分页接口和网络；私有 API 直链不含 Token，接收者必须自行拥有对应仓库权限。

### 2026-08-24 — 原生亚克力、低开销动画与递归闲时缩略图

- `modelscope_manager/app.py`、`modelscope_manager/windows_effects.py`、`modelscope_manager/styles.py`：移除复杂页面的 `QGraphicsOpacityEffect` 软件合成，页面切换不再保留旧帧；侧栏从同时动画最小/最大宽度改为仅对最大宽度执行 140ms 动画。账户表格填充期间冻结重绘并使用固定 42px 行高，避免启动时短暂错位。
- `modelscope_manager/app.py`、`modelscope_manager/windows_effects.py`：主题设置新增 GPU 加速和 Blur 亚克力半透明开关。GPU 开关在下次启动前选择 Qt 桌面 OpenGL 或软件 OpenGL；亚克力使用 Windows 11 DWM 系统背景或 Windows 10 `SetWindowCompositionAttribute`，由桌面合成器处理，失败时回退普通半透明。
- `modelscope_manager/app.py`：设置页重排为基本设置、主题设置、账号设置、下载设置（含 aria2-next）、播放设置、WebDAV 设置、索引和预览；原有开机自启、限速、PotPlayer、复制阈值等功能均保留。
- `modelscope_manager/app.py`、`tests/test_app_helpers.py`：缩略图改为递归闲时队列，当前目录优先。仓库超过 100 项时每批 4 项、最多 2 线程、间隔 900ms；较小仓库每批 16 项、最多 4 线程。用户键盘、鼠标或滚轮操作会推迟下一批，失败项不会无限重试。
- `README.md`、`tests/test_styles.py`：同步设置路径、亚克力/GPU 和递归缩略图行为，增加亚克力样式与 100 项限速边界测试。
- 验证：`compileall` 与全量单元测试通过（69 tests）；离屏检查确认 7 个设置卡片顺序、账户表格一次性重绘、页面无图形特效、侧栏单属性动画和递归队列深度顺序。使用用户授权的只读 Token 读取 `ARXChem/Animations-List` 当前 2298 项（1847 个媒体）耗时 7.65 秒；首批 4 个小图片在 2 线程下 2.35 秒完成 3 个，1 个 `.jpg` 重试仍不可解码并正确回退。Token 未写入源码、配置、日志或本文。
- 风险：Computer Use 无法为需要 `pythonw.exe main.py` 参数的源码启动方式创建目标窗口，因此真实 Windows 前台的 DWM 模糊观感未通过自动化截图确认；离屏截图只能验证布局。不同 Windows 版本会使用 DWM 亚克力或兼容回退，GPU 开关需重启后生效。

### 2026-08-24 — 缩略图、搜索排序、主题动画与同路径复制修正

- `modelscope_manager/app.py`：缩略图画框改为 208×117 的 16:9 区域，卡片使用 224×154 为下方标题保留同宽空间；新缩略图统一等比缩放并补边到 16:9。进入目录后会在后台处理该目录全部直属媒体，不再要求先切换到缩略图模式。
- `modelscope_manager/app.py`、`modelscope_manager/styles.py`：复制阈值改用“增大/减小”按钮；页面切换增加 180ms 淡入，侧栏展开/收起增加 220ms 宽度动画；公开资源搜索页展示完整条目并支持按名称、类型、真实大小和路径排序；缩略图列表加入明确的浅色/深色样式与完整调色板刷新，避免连续切换主题后背景反色。
- `modelscope_manager/app.py`、`tests/test_app_helpers.py`：同仓库同父目录粘贴时，本地临时文件与远端目标都增加 `-copy`；文件保留扩展名（如 `frame-copy.png`），文件夹使用 `folder-copy/...`，避免写回原路径失败。
- 验证：`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过；全量单元测试通过（67 tests）；离屏检查确认缩略图尺寸、深浅色列表背景、公开搜索大小排序、页面与侧栏动画参数；复制回归测试实际覆盖临时下载和上传目标。
- 风险：缩略图现在会在每次首次进入目录时后台扫描该目录直属媒体；大目录可能产生较高 CPU、磁盘和网络占用，可通过既有线程数设置降低并发。动画已离屏验证参数，尚未在不同刷新率的真实前台窗口逐帧观察。

### 2026-08-24 — 隐藏并发缩略图任务与 16:9 固定卡片

- `modelscope_manager/app.py`：缩略图任务改为线程池并发执行，默认 32 个工作线程；设置 → 预览新增“缩略图生成线程数”，范围 1–128，使用既有“增大/减小”按钮并持久化设置。所有 FFmpeg 子进程使用 `CREATE_NO_WINDOW`、`-hide_banner` 和 `-loglevel error`，缩略图模式不再弹出命令行窗口。
- `modelscope_manager/app.py`：缩略图网格项固定为 224×126（16:9），缩略图为 208×90；使用自适应图标网格，窗口变宽时增加同尺寸卡片列数，不拉伸单个卡片。可见区域预取范围随卡片尺寸同步调整。
- 验证：`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过；`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（65 tests）；离屏构造检查确认网格为 16:9，默认线程数为 32。
- 风险：32 个并发 FFmpeg/网络任务可显著占用 CPU、磁盘与带宽；用户可在设置中降低线程数以适配较弱设备。

### 2026-08-24 — 大目录流畅浏览、按需视频缩略图与右键补齐

- `modelscope_manager/app.py`：左侧隐藏目录状态树不再为每个文件创建节点，只构建目录；右侧直属目录结果按路径缓存，详细模式不再创建隐藏的缩略图项目，并在批量填充时暂停重绘。大目录切换和按列排序仅处理当前目录项目。
- `modelscope_manager/app.py`：缩略图模式仅在视口项目及相邻预取范围内、停止滚动 250ms 后生成缩略图；快速滚动会请求中断旧批次。视频不受图片大小阈值限制，FFmpeg 从第 0 秒提取首帧并限制探测数据；缩略图继续永久保留于 `data/thumbnails`，不会影响文件移动、删除或重命名。
- `modelscope_manager/app.py`：右侧详细/缩略图空白区域增加右键“粘贴”，没有复制来源或当前仓库只读时为禁用状态；左侧仓库目录树文件夹也可通过右键使用既有文件菜单。预览大小设置改用“增大/减小”按钮；侧栏三横线居中，收起时保留品牌和版本占位高度，导航控件 Y 坐标不再上移。
- 验证：使用提供的只读令牌读取 `ARXChem/Animations-List`，完整清单 2234 项、`! LibVVENC…` 直属项目 704 项，SDK 读取 6.67 秒；该真实清单离屏构建目录树 0.005 秒、右侧渲染 0.008 秒、按大小排序 0.007 秒。真实视频首帧缩略图生成成功并永久写入本地缓存。`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 与 65 项单元测试均通过。
- 风险：真实视频验证依赖系统 PATH 可找到 `ffmpeg.exe`；当前开发机可用，便携包仍未内置该可执行文件。

### 2026-08-24 — 修复加载目录闪退与跨目录残留项

- `modelscope_manager/app.py`：移除 Python 自定义 `QTreeWidgetItem.__lt__`。当前便携版 PySide6 在 Qt 排序回调进入该 Python 虚方法时会直接终止进程，表现为加载路径时应用闪退；详细信息和搜索结果现改为在填充控件前进行 Python 层排序，大小仍按真实字节数排序。
- `modelscope_manager/app.py`：直属目录项严格匹配 `当前目录/` 前缀，不再按路径字符串长度截取。此前进入 `folder` 时根目录 `root.bin` 会被误显示为 `in`，这也是随机目录/根目录残留部分字母的根因；新增 `_direct_remote_entries` 作为独立可测试的过滤逻辑。
- `tests/test_app_helpers.py`：增加跨目录路径不会出现在当前目录列表的回归测试。
- 验证：`QT_QPA_PLATFORM=offscreen` 下验证“加载目录→进入文件夹→按大小排序→缩略图”通过；`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过；`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（65 tests）。
- 风险：尚未通过真实前台窗口连接远端仓库进行连续快速切换目录的人工测试，但导致进程终止的排序回调与错误路径截取均已有确定复现和回归覆盖。

### 2026-08-24 — 资源目录状态、排序与图标视图修正

- `modelscope_manager/app.py`：右侧资源浏览改以独立的 `current_directory_path` 作为唯一目录状态；仓库/目录切换、刷新与文件夹大小索引重建不再从已清空或旧的隐藏树节点反推路径，修复右侧残留错误文件名。文件夹在详细列表中读取持久化大小索引，大小列按真实字节数排序。
- `modelscope_manager/app.py`：搜索结果（独立搜索窗口及页面结果）支持点击列头排序，大小按真实字节数排序；“大小”分组统一将文件和文件夹归入 `<1MB`、`1MB-1GB`、`>1GB`，尚未有缓存的目录显示“未索引”。缩略图模式改为文件管理器式方形图标网格，图标下方显示名称。
- `modelscope_manager/app.py`：仓库读取、更新索引、刷新目录移至资源页顶栏；侧栏三横线移到品牌下方，导航整体下移，版本号居中，设置图标与文字间距与普通导航一致。
- `tests/test_app_helpers.py`：覆盖大小分组的 1 MB 与 1 GB 边界。
- 验证：`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过；`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（64 tests）；`QT_QPA_PLATFORM=offscreen` 下完成资源目录、大小分组和缩略图网格构造检查。
- 风险：尚未在真实前台窗口连接大型远端仓库，确认超长文件名在不同 Windows 字体回退下的最终截断效果。

### 2026-08-24 — 验证状态链路与单栏资源浏览

- `modelscope_manager/app.py`：修复移除资源页标题时一并删除 `repo_heading` 状态承载控件造成的异常；该异常发生在“保存并验证账户”的任务启动前，因此界面会永久停留在“正在验证令牌”。该控件现为不可见状态承载，认证成功后通过下一事件循环再开始仓库读取，避免两个后台任务争用任务状态。
- `modelscope_manager/app.py`：目录树收回左侧仓库导航，右侧只保留标准详细信息单栏（名称、类型、大小），不再展示路径列或双栏目录树。Public 仓库读取完成后同样会填充左侧目录与右侧根目录内容。
- `modelscope_manager/app.py`：搜索窗口结果新增右键菜单，复用预览、下载、复制及既有链接/标签操作；复制文件夹时会从缓存取得同仓库完整条目集合。
- 验证：`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过；`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（63 tests）；离屏检查确认 Public 条目显示于单栏详情视图。
- 风险：未在真实前台窗口执行带网络的完整“验证→读取仓库”交互；网络超时由 ModelScope SDK 的请求策略决定。

### 2026-08-24 — 认证诊断、按需预览与跨仓库复制

- `modelscope_manager/app.py`：使用提供的只读令牌完成一次官方 API 只读 `whoami` 验证，确认令牌有效；先前失败来自受限执行环境拒绝 HTTPS 套接字，而非认证错误。令牌未写入源码、项目记录或日志。
- `modelscope_manager/app.py`、`modelscope_manager/storage.py`：预览设置新增“自动对大小小于…MB 的图片生成缩略图”。缩略图仅在用户打开文件夹且处于缩略图模式时按需生成；图片以内存字节流交给 FFmpeg 后立即释放，视频直接以远端 URL 抽取首帧，均只保留 `data/thumbnails` 的最终缩略图。现有缓存命中后不重复生成。
- `modelscope_manager/app.py`、`modelscope_manager/folder_index.py`：右键新增“复制/粘贴”。复制来源可为账户或 Public；粘贴到可写目录时，小于等于设置阈值的资源立即后台下载、上传和清理临时目录，大于阈值先显示确认。文件夹复制仅重算并持久化选中目录的大小，不刷新全仓库索引。
- `tests/test_folder_index.py`：增加仅更新选中目录索引的持久化测试。
- 验证：只读 API 验证返回账户 `ARXChem`；`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过；`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（63 tests）；离屏检查确认预览及复制阈值控件默认值。
- 风险：视频缩略图仍要求系统 PATH 有 `ffmpeg.exe`；尚未针对真实大目录执行跨仓库下载/上传端到端复制，以避免写入远端仓库。

### 2026-08-24 — 资源管理器功能区、目录详情与可收起导航

- `modelscope_manager/app.py`：最左侧导航增加三横线收起/展开按钮；收起后仅保留各页面图标和悬停提示。资源管理页移除标题、副标题及页内筛选栏，把搜索移至独立窗口（第一行提供全部仓库、类型、标签等条件）。
- `modelscope_manager/app.py`：仓库读取后保留任意层级可展开目录树；选中路径时，右侧“详细信息”视图仅列出该路径直接包含的名称、类型和大小，并可按表头排序、按名称/类型/大小分组。顶部新增 Windows 10 风格的“查看”模式切换、紧凑视图，以及新建文件夹、下载文件、网页端管理/删除入口；读取/刷新仓库、更新索引、刷新目录合并为左侧横向三个按钮。
- `modelscope_manager/app.py`、`modelscope_manager/storage.py`：缩略图模式会将图片缩略图写入 `data/thumbnails`；视频缩略图优先使用系统 PATH 中的 `ffmpeg.exe` 抽取首帧，缺少该程序时不阻断浏览并回退到普通文件显示。
- 验证：`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过；`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（62 tests）；`QT_QPA_PLATFORM=offscreen` 下成功构造主窗口并验证侧栏收起状态与右侧三列表格。
- 风险：未连接真实 ModelScope 仓库验证受鉴权链接的图片/视频缩略图下载；视频首帧依赖系统提供的 `ffmpeg.exe`，当前便携包未包含该可执行文件。

### 2026-08-23 — 左侧导航视觉调整

- `modelscope_manager/app.py`：资源管理、资源搜索及其余非设置导航图标统一为 20 pt；设置图标仍为 13 pt，并左移约一个空格宽度以拉开与文字的距离；底部仅显示版本号 `1.0.1`。
- 验证：`runtime\\python.exe -m compileall -q main.py modelscope_manager tests` 通过；离屏导航控件几何检查通过（普通导航 20 pt、设置 13 pt、设置图标 x=9）；`runtime\\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（62 tests）。
- 风险：尚未在真实 Windows 前台窗口中人工确认各字体回退时的最终像素间距。

### 2026-08-23 — 导航、Public 历史与端口探测修正

- `app.py`：恢复“资源管理”文字字号；导航按钮改为独立图标标签，资源管理图标使用 20 pt（相对原 13 pt 约放大 50%），设置与其余导航文字不受影响。
- `app.py`、`public_pools.py`、`database.py`、`folder_index.py`：搜索历史增加单项删除和清空；Public 仓库右键增加“移除/打开网页端”，账户仓库仅有“打开网页端”。移除会同步清理公开池、本地条目、标签和文件夹大小索引。
- `app.py`：搜索历史窗口改为无主窗口的普通非模态窗口，避免持续覆盖主窗口；WebDAV 启动前探测端口，发生冲突时自动选择相邻端口并显示非阻塞顶部提示；设置页新增“本页内容自动保存”说明。
- `README.md`：同步历史删除、Public 移除和端口探测说明；扩展 `test_app_helpers.py` 与 `test_public_pools.py`。
- 验证：`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（62 tests）；`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过；离屏导航图标构造检查通过。
- 风险：未在真实 Windows 前台环境验证端口冲突提示的视觉位置，也未手动验证无主搜索历史窗口在多显示器场景的焦点行为。

### 2026-08-23 — 后续界面、标签与 Public 挂载

- `app.py`、`styles.py`：资源管理导航图标放大；为设置滚动区指定浅色/深色背景，主题即时切换不再需要重启；关闭紧凑视图后仓库栏最小宽度降为 180，恢复分隔条拖拽调整。
- `database.py`、`app.py`：新增本地多标签表和右键“标签”菜单，可新建或切换多个标签；资源管理顶栏增加标签筛选。完整条目缓存会删除远端已消失路径的标签映射和无引用标签。
- `app.py`、`public_pools.py`：资源管理仓库树新增只读 `Public` 根节点，搜索成功的公开仓库会保存本地条目和文件夹大小索引；后台默认不更新 Public，手动“更新索引”才刷新。
- `app.py`：资源搜索增加非模态独立“搜索历史”窗口；WebDAV 配置字段自动保存，移除启用开关，改为“启动程序后自动启动监听”及独立“启动/停止”按钮。
- `README.md`：同步标签、Public、搜索历史和 WebDAV 行为说明；扩展数据库标签测试。
- 验证：`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（60 tests）；`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过；标签数据库冒烟检查通过。
- 风险：未连接真实公开仓库验证首次 Public 缓存与手动更新，也未进行鼠标拖拽分隔条或系统主题切换的可见交互测试。

### 2026-08-23 — V1.0.1

- 初始化本地 Git 仓库（未创建提交），并保持现有 `data/`、缓存和 Python 字节码忽略规则不变。
- `modelscope_manager/styles.py`、`app.py`、`locales/en_US.json`：新增浅色/深色主题和“跟随系统”选项，默认跟随系统并监听系统颜色方案变化；`QTimeEdit` 纳入两套主题样式，修复深色主题下限速开始/结束时间不可见。
- `app.py`：上传改为逐项顺序队列；每项记录独立目标路径并可在表格中编辑。上传中继续拖放/选择项目会追加为“等待”，状态列显示百分比、完成、失败、取消或跳过；当前项支持暂停、恢复和取消。暂停/取消在 ModelScope SDK 的数据读取回调边界生效，取消不会删除已上传的远端内容。
- `folder_index.py`、`app.py`：目录树显示上次索引缓存的文件夹大小；无缓存显示 `--`，索引线程完成对应仓库时刷新为最新值。普通目录读取不再把即时浏览结果写成大小索引。
- `README.md`：同步主题、上传队列和文件夹大小显示说明；新增 `test_upload_thread.py`、`test_styles.py`，并扩展 `test_folder_index.py`。
- 验证：`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（59 tests）；`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过；JSON 和两套主题样式加载检查通过。
- 风险：未在真实 ModelScope 大文件上传中验证 SDK 回调的暂停/取消时延；该时延取决于 SDK 下一个数据读取回调。未执行首次 Git 提交，也未进行真实系统深色主题、WebDAV 或 aria2 的端到端交互测试。

### 2026-08-23

- 新建本文件，完成主项目核心结构、按文件列表功能说明、运行链路、部署数据边界、关键安全/分页/限速约束和验证方式整理。
- 验证：已读取根目录、业务包、测试目录、README 和部署目录清单；`runtime\python.exe -m unittest discover -s tests -p "test_*.py"` 通过（55 tests）；`runtime\python.exe -m compileall -q main.py modelscope_manager tests` 通过。
- 风险：当前项目目录没有可用 Git 元数据；远端 ModelScope 服务、真实 aria2 下载、真实 WebDAV 客户端和实际 PotPlayer 播放链路仍需在后续相关改动中单独验证。
