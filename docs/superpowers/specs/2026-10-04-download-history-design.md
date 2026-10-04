# 下载历史账本与失败重下 — 设计文档

- 日期：2026-10-04
- 状态：待评审
- 范围：SQLite 持久化下载账本 + 历史界面 + aria2 终态回填 + 单文件重下 + 保留策略设置

## 1. 目标

让"下载到本地"这件事从一次性的内存快照变成一个**可回溯、可补救的账本**：

1. 重启后仍能看到哪个任务、哪天、下了哪些文件、成功还是失败、多大、落在哪里；
2. aria2 模式的下载不再"投递完就消失"，完成/失败能被回填到账本里；
3. 界面上进行中与历史分开，不再被旧记录挤出重点；
4. 从历史能看出文件是否真的还在本地；
5. 单条失败记录能直接重下，不必重跑整个任务。

动机（用户原话）：本地下载失败时希望能重新拉取下载任务进行下载。

## 2. 非目标（本期明确不做，YAGNI）

- **批量重下**：不做"一键重下本次运行所有失败项"，只有单条重下。因此账本不引入 `run_id` 分组字段。
- **前置失败不落库**：`取直链失败`、`驱动不支持下载`、`目录递归为空`、`aria2 投递失败` 这类还没建立 job 就挂掉的情况，
  继续只出现在任务日志里，账本里没有对应记录。账本记的是"下载动作的完整生命周期"，不是"下载意愿的清单"。
- **后台轮询 aria2**：不起常驻 job 拉 aria2 状态，改为查询时对账（见 4.4）。
- **任意记录都可重下**：不做"仅失败记录可点重下"的状态判断。任意历史记录的按钮都可点，文件若已在且同大小会自然进 `skipped`；
  少一组按状态分支的禁用逻辑，行为也可预期（点完成记录 = 确认文件还在）。
- **改动转存的串行锁**：下载本来就跑在全局转存锁之外（`task_service.py:177`），重下同样只走下载段、不触发转存，
  不碰这把锁。

## 3. 现状

- `backend/core/download_registry.py`：内存注册表。`_active` 存进行中，`_done` 是 `maxlen=200` 的环形队列，
  `snapshot()` 把两者拼在一起返回。文件首行注释即"进程重启即清空"。
- `backend/services/download_service.py:264` `aria2_status()`：只查 `aria2.tellActive` 与 `aria2.tellWaiting`，
  不查终态。**aria2 下载一旦结束就从界面消失，且不留任何痕迹**——`_aria2_submit()` 全程不碰 registry，只输出日志行。
- `backend/api/routes_downloads.py`：`GET /api/downloads` 返回 内存终态 + aria2 进行中 的混合列表。
- `frontend/src/views/DownloadsView.vue`：单张 `el-table` 平铺上面那份混合列表，1.5s 轮询，无筛选无分组。
- `backend/models.py`：只有 `account` / `task` / `setting` / `external_api_token` 四张表，无下载记录。
- 因此：**没有任何形式的持久下载历史**。

## 4. 设计

### 4.1 数据模型

新表 `download_record`（加在 `backend/models.py`，由 `init_db()` 的 `create_all` + `_auto_add_columns()` 自动建，无需手写迁移）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | int PK | |
| `source` | str | `builtin` / `aria2`，决定后续操作与对账走哪条路 |
| `ref_id` | str, index | builtin 存 registry 的 job_id；aria2 存 gid。对账与 RPC 回填靠它 |
| `task_id` | int \| None | **只存值、不加外键约束**：任务被删后历史仍要留 |
| `taskname` | str | 落库即快照，不随任务改名而变 |
| `filename` | str | |
| `dest_path` | str | 本地绝对路径（重下的目标位置） |
| `size_total` | int | |
| `size_done` | int | 失败时保留已写字节数 |
| `fid` | str | 转存后的网盘 fid，重下取直链用 |
| `driver_key` | str | `account_id` 失效时按此回落选账号 |
| `account_id` | int \| None | 取直链用的账号 |
| `status` | str, index | 沿用 registry 词表：`queued`/`downloading`/`done`/`failed`/`skipped`/`stopped` |
| `error` | str | |
| `created_at` | NaiveDatetime | 记录建立时间 |
| `finished_at` | NaiveDatetime \| None | 进终态时间；`prune()` 按它淘汰 |

**不存 `file_state`**：文件在不在是随时间变化的事实，写进账本会把记录污染成一次性快照。它作为查询时的派生字段返回（见 4.3），
取值 `ok` / `missing` / `unknown`（不用布尔值，因为"读不到"既不是存在也不是丢失，必须能表达第三种）。

### 4.2 分层与新模块

`backend/services/download_history.py` 是账本的唯一出入口，五个动作：

```python
start(*, source, ref_id, task_id, taskname, filename, dest_path, size_total, fid, driver_key, account_id) -> int
    # 落一条 status=queued
finish(ref_id, *, source, status, size_done=None, size_total=None, error="") -> None
    # 按 ref_id 定位，更新为终态并写 finished_at
list_records(*, page, page_size, status, task_id, keyword) -> dict   # 内含 reconcile + 当前页 stat
reconcile() -> None                                                  # 收口所有非终态记录
prune(mode: str) -> int                                              # "auto" | "all" | "failed"
```

顺带清理一处既有边界问题：`download_service._Item`（`download_service.py:57`）现在提为公开的 **`DownloadItem`**——
它要跨模块传给 `download_history` 和 `download_items()`，让 services 层的公开接口吃一个下划线私有类型是说不通的。字段不变（`fid`/`name`/`size`/`local_path`）。

- `backend/core/download_registry.py` **保持零 DB 依赖**（当前 `core/` 全目录不 import `models`/`database`，不破坏这层）。
  只做两处改动：新增 `get(job_id) -> DownloadJob | None`（终态后对象仍在 `_done` 里，取出来写库），
  `snapshot()` 不再拼接 `_done`，只返回进行中。
- `download_service` 只多两处调用，见 4.5。
- 依赖方向：`services/download_history.py` → `models` / `database` / `drivers`；被 `download_service` 与 `routes_downloads` 调用。

### 4.3 API 契约

| 端点 | 变化 | 说明 |
| --- | --- | --- |
| `GET /api/downloads` | 语义收窄 | 只返回进行中（内置 `snapshot()` + aria2 `tellActive/tellWaiting`）。已确认唯一消费者是 `DownloadsView.vue`，外部 API 未暴露，改动不外溢 |
| `GET /api/downloads/history` | 新增 | 查 DB。`page`(默认 1) / `page_size`(默认 50，上限 200) / `status`(逗号分隔多选) / `task_id` / `keyword`(匹配 filename、dest_path)。按 `created_at` 倒序，返回 `{items, total}`，每条附派生字段 `file_state`: `ok` / `missing` / `unknown` |
| `POST /api/downloads/history/{id}/retry` | 新增 | 单条重下，见 4.6。**起后台任务、不 await 下载**，立即返回 `{ok: bool, message: str}` |
| `DELETE /api/downloads/history/{id}` | 新增 | 删记录，**不动磁盘文件** |
| `POST /api/downloads/history/prune` | 新增 | body `{mode: "auto" \| "all" \| "failed"}`，返回删除条数 |
| `POST /api/downloads/{job_id}/{stop,pause,resume}`、`DELETE /api/downloads/{job_id}` | 不变 | 仍作用于内存 registry / aria2 |

「进行中」的删除仍只清内存显示，「历史」的删除清 DB，两者语义分开，不互相代理。

**`file_state` 不阻塞 event loop**：stat 是阻塞 IO，NAS 挂载慢时串行 50 次会拖死请求。用 `asyncio.to_thread` 对当前页并发 stat，
单条 `OSError` 归为 `unknown` —— 不把"读不到"说成"文件没了"。

**重下为什么不能同步**：内置下载器下一个 4K 文件在免费盘上可能要几小时（见既有性能记录），HTTP 请求挂那么久必然被代理/浏览器掐断，
用户看到的是"点了没反应"。所以端点只做校验 + 起 `asyncio.create_task(download_items(...))`，立刻返回。
因为 `start()` 在取到直链、建立 job 时就落库，几秒内刷新历史就能看到那条 `queued`，点了不是一片空白。

### 4.4 非终态记录对账（`reconcile()`，无后台轮询）

触发时机：`list_records()` 执行前，对 DB 里**所有**非终态（`queued` / `downloading`）记录跑一次。这类记录数量等于未完成任务数，本来就不多。
账本记的是状态跳变（`start` / `finish`），**不做进度写盘**——内置的实时速度、已下字节仍只在内存 registry 里，避免每 0.25s 打一次 SQLite。

按 source 分派，共同兜底：

1. `source=aria2`：先 `aria2.tellActive` / `aria2.tellWaiting` 确认哪些 gid 仍在跑 → 这些保持不动；其余用一次 `system.multicall` 问
   `aria2.tellDownloadResult`，`complete` → `done`，有 `error` → `failed`，顺带回填 `size_done` / `size_total` / `finished_at`；
2. `source=builtin` 且 `ref_id` 在内存 registry 里仍是 active job：本进程还在下，**保持不动**（进行中 tab 负责展示它）；
3. 上述问不到（gid 被 aria2 丢弃、进程重启导致 registry 清空、RPC 不可达）→ stat 目标文件，存在且大小与 `size_total` 一致（或 `size_total` 为 0 且非空）→ `done`；
4. 仍不确定：以 `created_at` 为基准，距今 **< 24h 保持 `queued`**（可能真在下），**≥ 24h 判 `failed`**，
   `error` = `"对账超时：下载器无响应或结果已丢弃"`。

第 4 条同时收口两种悬空：重启后遗留的 aria2 投递，和重启时被打断的内置下载。账本里不留永久非终态记录。

### 4.5 记录生命周期与写入点

两条源都走 `start()` → `finish()` 两个点：

- 内置 `_builtin_download()` 的 `one()`：`registry.create()` 拿到 job_id 后 `start(source="builtin", ref_id=job_id, ...)`；
  `_fetch_one` 返回后 `registry.get(job_id)` 取终态快照，`finish(...)` 写 `done`/`failed`/`skipped`/`stopped`。
- aria2 `_aria2_submit()`：`addUri` 拿到 gid 后 `start(source="aria2", ref_id=gid, ...)`（需要把 `task_id` / `taskname` 传进该函数，目前没传），
  终态由 `reconcile()` 回填。
- 取直链失败（两条源都是）→ 连 `start()` 都不到，不落库。与第 2 节的"前置失败不落库"一致。
- 两处写入都包 `try/except`，失败只 `log("warn", ...)`。**账本是旁路观测，主流程绝不因它中断**，
  写库异常不改变 `_builtin_download` / `_aria2_submit` 的返回值。
- Emby 刷新、通知聚合、日志行等既有行为不变。

**一条已知的重叠，是预期行为**：正在下载的文件会同时出现在「进行中」（内存 registry，实时进度）和「历史」（DB，`queued` 状态）两个 tab。
前者是实时视图，后者是全生命周期账本，不追求互斥。

### 4.6 重下链路与 `download_items()` 抽取

现存结构问题：`download_task_files()`（`download_service.py:114`）把"SavedFile → 待下载清单"和"清单 → 执行下载"焊在一起。
重下天然拿到的是**已知的本地目标路径**，不该再被 `resolve_local()` 按网盘目录重算一遍（配置的 `dir` 可能已变、`savepath_override` 语义会二次作用）。
因此抽出两个函数：

```python
download_items(driver, items: list[DownloadItem], cfg, *, log, task_id, taskname) -> list[str]
    # 分派 builtin/aria2、落库、Emby 刷新
download_task_files(driver, saved: list[SavedFile], cfg, ...) -> list[str]
    # 保持原语义：collect_files() + download_items()
```

重下即：读记录 → 按 `account_id` 构造 driver（账号被删/禁用则按 `driver_key` 回落 `_primary_account`，复用 `routes_files.py:21` 的选择逻辑）
→ `get_download_urls([fid])` 取新直链（旧直链已过期）→ 造 `DownloadItem(fid, filename, size_total, Path(dest_path))` → `download_items()`。

于是重下自动享有当前的下载模式、并发、`.part` 保护、已存在跳过的语义，并且走同一个写入点落新记录，不产生第二套真相。
直链仍取不到（文件已不在网盘）→ 返回失败摘要并落一条 `failed`，界面看得到原因。

### 4.7 界面

`frontend/src/views/DownloadsView.vue` 拆成两个 tab（不是三个）：

- **进行中**（默认）：现有表格原样保留（进度条、速度、停止/暂停/继续），1.5s 轮询 `GET /api/downloads`，tab 标签带数量角标。
- **历史**：`GET /api/downloads/history`。列 = 任务 / 文件 / 体积 / 状态 / **文件**（在·已丢失·未校验）/ 完成时间 / 目标路径 / 操作（重下、删记录）。
  顶部筛选 = 状态多选 + 任务下拉 + 关键词，底部 `el-pagination`。

拆两个而非「进行中/已完成/失败」三个的原因：完成、失败、已停止、跳过都是同一张表上的 `status` 过滤条件，
拆 tab 等于复制三遍表格代码，还要额外决定"已停止"归属哪个 tab；一个状态下拉就覆盖了。

历史 tab 不进 1.5s 轮询。拉取时机只有三种：切入 tab、翻页/改筛选、点重下之后。
当当前页存在非终态（`queued` / `downloading`）记录时挂一个 5s 定时器，切走即清。

### 4.8 保留策略

- 设置项 `download.history_retention`：`"days_30"` / `"days_90"` / `"days_180"` / `"forever"`，默认 **`days_90`**。
  加在 `deps.py` 的 `DEFAULT_SETTINGS["download"]`，UI 在 `SettingsDownload.vue` 加一行下拉。
- 非 `forever` 时由既有 APScheduler 跑清理：**启动后一次 + 每天一次**，删除 `finished_at` 早于阈值的记录（含已终态记录；非终态记录不受影响，交给对账收口）。
- `forever` 时跳过自动清理，只靠手动入口。
- 手动清理在历史 tab：一个「清理」下拉 —— 按当前保留策略清理 / 只清失败记录 / 清空全部（二次确认）。

这是本项目"少配置、多默认"方向下唯一新增的配置项，且只有一个下拉。

## 5. 改动清单

- `backend/models.py`：新表 `download_record`
- `backend/services/download_history.py`：新增
- `backend/services/download_service.py`：抽 `download_items()`、`_Item` 提为公开 `DownloadItem`、`start`/`finish` 两处调用、`_aria2_submit` 接收 task 信息
- `backend/core/download_registry.py`：新增 `get()`，`snapshot()` 去掉 `_done`
- `backend/api/routes_downloads.py`：history / retry / delete / prune 端点
- `backend/api/deps.py`：`history_retention` 默认值
- 调度注册处：每日清理 job
- `frontend/src/views/DownloadsView.vue`：双 tab
- `frontend/src/api/{client,types}.ts`：新接口与类型
- `frontend/src/components/settings/SettingsDownload.vue`：保留策略下拉
- `README.md`：下载章节补历史与重下说明

## 6. 测试

`backend/tests/conftest.py` 已把 `DATA_DIR` 隔离到临时目录，新表自动建在临时库，不会碰真实 DB（此项曾毁掉真实任务，见既有约定）。
aria2 沿用 `test_download.py` 的打桩方式（`monkeypatch.setattr(dl.httpx, "AsyncClient", FakeClient)` + `aria2_reachable`），不依赖真实 daemon。

新增 `backend/tests/test_download_history.py`：

- 生命周期完整：内置走 `start`→`finish` 只留一条记录（不是两条），`fid`/`driver_key`/`dest_path`/`ref_id` 齐全；aria2 投递后停在 `queued`；
- `reconcile()` 分支：aria2 `tellDownloadResult` 回 `complete` → `done`；gid 仍在 `tellActive` → 保持不动；gid 查不到但文件同大小 → `done`；
  builtin 且 registry 里仍 active → 保持不动；builtin 且 registry 已无此 job（模拟重启）+ 无文件 + ≥ 24h → `failed` 且 error 文案正确；< 24h → 保持 `queued`；
- `file_state` 三态：`ok` / `missing` / stat 抛 `OSError` → `unknown`；
- `prune()`：阈值内删除、`forever` 不删、`failed` 只删失败；
- **写库失败不冒泡**：`session_scope` 抛异常时 `download_items()` 仍返回正确结果行，且不留下半截记录之外的影响。

扩充 `backend/tests/test_download_api.py`：

- `GET /api/downloads` 不再返回终态记录；
- history 的状态筛选 + 关键词 + 分页与 `total`；
- retry 立即返回（断言响应里不含下载耗时逻辑），后台任务跑完后新记录的 `dest_path` 与原记录**完全一致**（证明没被 `resolve_local` 重算）；
- delete / prune 端点。

回归：`test_download.py` 现有 aria2 协议用例应全绿（`_aria2_submit` 多了参数和两次落库调用，不改 payload）。

前端：`npm run build` 过类型检查，再做真机端到端验证——内置模式跑任务看历史出条、切 aria2 看 `queued`→`done`、
手动删掉已下文件后刷新看「已丢失」、点重下确认文件回来、重启容器确认历史仍在。报告中会明确区分已验证与未验证项。

## 7. 分阶段交付

每阶段独立可验证、独立提交：

1. 表 + `download_history` 服务（`start`/`finish`/`prune`）+ 内置与 aria2 的写入点 + `registry.get()` + `DownloadItem` 提公开
2. history / prune 端点 + `/api/downloads` 语义收窄 + 历史 tab UI（筛选、分页、`file_state` 列）
3. `reconcile()` 接进 history 查询（aria2 终态 + 重启遗留收口）
4. `download_items()` 抽取 + retry 端点（后台任务）+ 重下按钮
5. 保留策略设置项 + 每日清理 job
