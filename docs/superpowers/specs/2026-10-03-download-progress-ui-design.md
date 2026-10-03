# 下载任务进度界面 — 设计文档

- 日期：2026-10-03
- 状态：待评审
- 范围：MVP（只读实时进度 / 仅内置下载器 / 内存态）

## 1. 目标

在 Web UI 提供一个「下载」视图，实时展示内置 httpx 下载器正在下载与最近完成的文件进度：
文件名、大小、已下载/百分比、速度、状态、目标路径。解决当前"下载在任务运行时 fire-and-forget、
界面完全看不到进度"的问题。

## 2. 非目标（本期明确不做，YAGNI）

- aria2 模式的进度（本机无 daemon 不可验证；留待后续用 `tellActive/tellStatus` 轮询）。
- 下载控制：暂停 / 继续 / 取消 / 重试。
- 持久化与历史：不入库，进程重启即清空（与"内存态"选择一致）。
- 断点续传（`.part` 已有保护，但不做"从中间恢复"的 UI 语义）。

## 3. 现状

- `backend/services/download_service.py`：`_builtin_download` → `_fetch_one` 以 64KB 分块写 `.part`，
  完成后 `os.replace` 改名；**只在结束时打一条日志**，无增量进度。
- 下载仅在任务运行且 `auto_download=True` 时触发，无独立任务态。
- 前端有 `LogsView` + SSE 日志流；`RunLogDialog` 用 POST-SSE 读运行日志。无下载视图。

## 4. 设计

### 4.1 数据模型（内存，不落库）

`backend/core/download_registry.py` 定义：

```python
@dataclass
class DownloadJob:
    id: str            # uuid4 hex 短码
    task_id: int | None
    taskname: str
    filename: str
    dest_path: str     # 本地绝对路径
    total: int         # 字节，未知为 0
    done: int          # 已写字节
    speed: float       # 字节/秒，滑动估算
    status: str        # queued|downloading|done|failed|skipped
    error: str = ""
    started_at: float
    updated_at: float
```

`DownloadRegistry`（模块级单例 `registry`）：
- `create(task_id, taskname, filename, dest_path, total) -> job_id`：插入 `queued`。
- `update(job_id, *, done=None, total=None, speed=None, status=None, error=None)`：就地更新，刷新 `updated_at`。
- `snapshot() -> list[dict]`：进行中（queued/downloading）在前、按 started_at 升序；其后是最近完成的
  （done/failed/skipped），按 updated_at 倒序。完成项存于 `deque(maxlen=200)` 的有界历史。
- 单事件循环内调用，无需加锁（更新都来自 asyncio 下载协程）。

### 4.2 内置下载器插桩

`_builtin_download` 为每个 `_Item` 先 `registry.create(...)` 拿 `job_id`，传入 `_fetch_one`：
- 开始 → `update(status="downloading")`。
- 写块循环内累计 `written`，每 ~0.3s（按 `time.monotonic()` 节流）`update(done=written, speed=...)`。
- 结束：完成→`done`；大小不符/HTTP 非 200/异常→`failed(error)`；已存在同大小跳过→`skipped`。
- `total` 用 `row["size"] or item.size`（夸克直链一般带 size）。

日志行为保持不变（`log("info", "📥 ...")`），注册表是额外通道。

### 4.3 后端接口

`backend/api/routes_downloads.py`：
- `GET /api/downloads` → `{"jobs": registry.snapshot()}`。只读。
在 `main.py` 挂载 `routes_downloads.router`。

### 4.4 前端

- 新增 `views/DownloadsView.vue`，路由 `/downloads`，`App.vue` 顶部导航加「下载」。
- 表格列：任务名 | 文件 | 进度条(done/total %) | 速度(MB/s) | 状态徽标 | 目标路径。
- 数据：`setInterval` 每 1.5s 调 `api.listDownloads()` 刷新；页面 `document.hidden` 时暂停轮询，
  重新可见立即拉一次；组件卸载清 timer。
- `api/client.ts` 增 `listDownloads: () => request<{jobs: DownloadJob[]}>("/api/downloads")`；
  `api/types.ts` 增 `DownloadJob` 接口。
- 进度条/状态样式沿用现有 `theme.css` 变量，与任务卡片观感一致。

## 5. 数据流

```
任务运行(auto_download) → download_service._builtin_download
   ├─ registry.create/update（done/speed/status）
   └─ 写 .part → os.replace
前端 DownloadsView ──每1.5s──> GET /api/downloads ──> registry.snapshot()
```

## 6. 边界与错误处理

- `total==0`（直链无 size）：进度条显示"未知大小"，仅显示已下字节与速度，不显示百分比。
- 并发：`concurrency` 个文件同时下，注册表并存多个 `downloading`。
- 任务被删除：下载中的 job 不回收（内存态，进程重启即清）；快照里 taskname 仍显示。
- 单文件失败：`status=failed` + `error`，不影响其它 job 与任务运行结果（与现状一致）。
- 快照有界：完成历史 `maxlen=200`，避免长跑内存增长。

## 7. 测试

- 后端单测（`backend/tests/test_download_registry.py`）：
  - registry：create→update→snapshot 排序与有界历史。
  - `_fetch_one` 插桩：mock 分块流，断言 `done` 递增、完成置 `done`、异常置 `failed`、已存在置 `skipped`。
  - `GET /api/downloads`：预置 registry 后返回快照结构。
- 前端：`npm run build`（tsc）通过。
- 真机验证：下 `backdrop.jpg`(~735KB) 等数百 KB 文件，0.1MB/s 下进度条可见移动→完成；
  打开 `/downloads` 截图确认。

## 8. 影响文件

新增：`backend/core/download_registry.py`、`backend/api/routes_downloads.py`、
`frontend/src/views/DownloadsView.vue`、`backend/tests/test_download_registry.py`。
改动：`backend/services/download_service.py`（插桩）、`backend/main.py`（挂路由）、
`frontend/src/router.ts`、`frontend/src/App.vue`、`frontend/src/api/client.ts`、`frontend/src/api/types.ts`。
