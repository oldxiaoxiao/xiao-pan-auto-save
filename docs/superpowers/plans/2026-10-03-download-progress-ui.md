# 下载任务进度界面（MVP）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Web UI 提供只读的「下载」视图，实时展示内置 httpx 下载器正在下载/最近完成文件的进度。

**Architecture:** 新增一个内存态 `DownloadRegistry` 单例；内置下载器在分块写盘时更新每个文件的 done/speed/status；`GET /api/downloads` 暴露快照；前端新增视图以 1.5s 轮询渲染进度条。aria2 模式、控制、持久化均不在本期。

**Tech Stack:** FastAPI + SQLModel（后端，Python ≥3.11，无新增依赖）、Vue3 + TypeScript + Element Plus + Pinia + vue-router（前端）、pytest + httpx（测试）。

**Spec:** `docs/superpowers/specs/2026-10-03-download-progress-ui-design.md`

## Global Constraints

- 不引入任何新的第三方依赖（仅 stdlib + 现有 fastapi/httpx/sqlmodel/Element Plus）。
- 后端 `ruff` 通过，line-length 110；测试用 `backend/tests/conftest.py` 的隔离 DATA_DIR。
- 下载进度为**内存态**，进程重启即清空；不做暂停/取消/重试；不入库。
- 仅内置（`mode=="builtin"`）下载器上报进度；aria2 分支不接入注册表。
- 前端文案中文，样式沿用 `theme.css` 变量与 Element Plus 组件。
- 每个任务以可独立测试的产物结尾并单独提交。

---

### Task 1: DownloadRegistry 内存注册表

**Files:**
- Create: `backend/core/download_registry.py`
- Test: `backend/tests/test_download_registry.py`

**Interfaces:**
- Consumes: 无。
- Produces:
  - `DownloadJob`（dataclass，字段见下）。
  - `class DownloadRegistry`：`create(*, task_id:int|None, taskname:str, filename:str, dest_path:str, total:int)->str`、`update(job_id:str, *, done:int|None=None, total:int|None=None, speed:float|None=None, status:str|None=None, error:str|None=None)->None`、`snapshot()->list[dict]`。
  - 模块级单例 `registry: DownloadRegistry`。
  - 状态常量集合 `ACTIVE = {"queued","downloading"}`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_download_registry.py
from backend.core.download_registry import DownloadRegistry


def test_create_update_snapshot_lifecycle():
    r = DownloadRegistry(history=2)
    a = r.create(task_id=1, taskname="T", filename="a.mp4", dest_path="/d/a.mp4", total=100)
    b = r.create(task_id=1, taskname="T", filename="b.mp4", dest_path="/d/b.mp4", total=0)
    r.update(a, done=50, speed=12.5, status="downloading")
    by_id = {j["id"]: j for j in r.snapshot()}
    assert by_id[a]["done"] == 50 and by_id[a]["status"] == "downloading"
    assert by_id[a]["speed"] == 12.5 and by_id[b]["status"] == "queued"

    r.update(a, done=100, status="done")
    snap = r.snapshot()
    ids = [j["id"] for j in snap]
    # 进行中(b)排在历史(a)之前
    assert ids.index(b) < ids.index(a)
    assert next(j for j in snap if j["id"] == a)["status"] == "done"


def test_history_is_bounded():
    r = DownloadRegistry(history=2)
    ids = [r.create(task_id=None, taskname="T", filename=f"{i}", dest_path="/d", total=1) for i in range(5)]
    for i in ids:
        r.update(i, status="done")
    done_ids = [j["id"] for j in r.snapshot()]
    assert len(done_ids) == 2 and ids[-1] in done_ids and ids[0] not in done_ids


def test_update_unknown_id_is_noop():
    r = DownloadRegistry()
    r.update("nope", done=5, status="done")  # 不抛异常
    assert r.snapshot() == []
```

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/pytest -q backend/tests/test_download_registry.py`
Expected: FAIL — `ModuleNotFoundError: backend.core.download_registry`

- [ ] **Step 3: 实现注册表**

```python
# backend/core/download_registry.py
"""下载进度内存注册表：内置下载器写盘时更新，前端轮询快照。进程重启即清空。"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field

ACTIVE = {"queued", "downloading"}


@dataclass
class DownloadJob:
    id: str
    task_id: int | None
    taskname: str
    filename: str
    dest_path: str
    total: int
    done: int = 0
    speed: float = 0.0
    status: str = "queued"  # queued|downloading|done|failed|skipped
    error: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class DownloadRegistry:
    def __init__(self, history: int = 200) -> None:
        self._active: dict[str, DownloadJob] = {}
        self._done: deque[DownloadJob] = deque(maxlen=history)

    def create(self, *, task_id: int | None, taskname: str, filename: str, dest_path: str, total: int) -> str:
        job = DownloadJob(
            id=uuid.uuid4().hex[:12],
            task_id=task_id,
            taskname=taskname,
            filename=filename,
            dest_path=dest_path,
            total=int(total or 0),
        )
        self._active[job.id] = job
        return job.id

    def update(
        self,
        job_id: str,
        *,
        done: int | None = None,
        total: int | None = None,
        speed: float | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        job = self._active.get(job_id)
        if job is None:
            return
        if done is not None:
            job.done = int(done)
        if total is not None:
            job.total = int(total)
        if speed is not None:
            job.speed = float(speed)
        if error is not None:
            job.error = error
        job.updated_at = time.time()
        if status is not None:
            job.status = status
            if status not in ACTIVE:
                self._active.pop(job_id, None)
                self._done.appendleft(job)

    def snapshot(self) -> list[dict]:
        active = sorted(self._active.values(), key=lambda j: j.started_at)
        return [asdict(j) for j in active + list(self._done)]


registry = DownloadRegistry()
```

- [ ] **Step 4: 运行验证通过**

Run: `.venv/bin/pytest -q backend/tests/test_download_registry.py`
Expected: PASS (3 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/core/download_registry.py backend/tests/test_download_registry.py
git commit -m "feat(download): 新增下载进度内存注册表"
```

---

### Task 2: GET /api/downloads 快照接口

**Files:**
- Create: `backend/api/routes_downloads.py`
- Modify: `backend/main.py`（import 列表 + `include_router`）
- Test: `backend/tests/test_download_api.py`

**Interfaces:**
- Consumes: Task 1 的 `backend.core.download_registry.registry`（`create/update/snapshot`）。
- Produces: `GET /api/downloads` → `{"jobs": [ <DownloadJob asdict>, ... ]}`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_download_api.py
from fastapi.testclient import TestClient

from backend.core.download_registry import registry
from backend.main import app


def test_downloads_endpoint_returns_snapshot():
    with TestClient(app) as client:
        jid = registry.create(task_id=9, taskname="x", filename="f.mp4", dest_path="/d/f.mp4", total=10)
        registry.update(jid, done=5, status="downloading")
        data = client.get("/api/downloads").json()
    job = next(j for j in data["jobs"] if j["id"] == jid)
    assert job["done"] == 5 and job["status"] == "downloading" and job["filename"] == "f.mp4"
```

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/pytest -q backend/tests/test_download_api.py`
Expected: FAIL — 404（路由未挂载）

- [ ] **Step 3: 实现路由**

```python
# backend/api/routes_downloads.py
"""下载进度快照接口（只读）。"""

from __future__ import annotations

from fastapi import APIRouter

from ..core.download_registry import registry

router = APIRouter(prefix="/api", tags=["downloads"])


@router.get("/downloads")
async def downloads() -> dict:
    return {"jobs": registry.snapshot()}
```

在 `backend/main.py` 的 `from .api import (...)` 里加 `routes_downloads,`，并在路由挂载区加：

```python
app.include_router(routes_downloads.router)
```

- [ ] **Step 4: 运行验证通过**

Run: `.venv/bin/pytest -q backend/tests/test_download_api.py`
Expected: PASS (1 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/api/routes_downloads.py backend/main.py backend/tests/test_download_api.py
git commit -m "feat(api): GET /api/downloads 下载进度快照"
```

---

### Task 3: 内置下载器插桩上报进度

**Files:**
- Modify: `backend/services/download_service.py`（`download_task_files`、`_builtin_download`、`_fetch_one`）
- Modify: `backend/services/task_service.py:176`（调用处传 `task_id` / `taskname`）
- Test: `backend/tests/test_download.py`（追加）

**Interfaces:**
- Consumes: Task 1 `registry.create/update`。
- Produces:
  - `download_task_files(..., *, download_subdir=False, savepath_override="", log=None, task_id:int|None=None, taskname:str="")`。
  - `_fetch_one(row, item, cookie_str, ua, *, job_id:str|None=None) -> tuple[bool,str]`：写盘时更新 registry，终态置 done/failed/skipped。
  - `_builtin_download(driver, items, cfg, log, *, task_id=None, taskname="")`：每个文件先 `registry.create`，把 `job_id` 传入 `_fetch_one`。

- [ ] **Step 1: 写失败测试（追加到 test_download.py）**

```python
@pytest.mark.asyncio
async def test_fetch_one_reports_progress_and_done(tmp_path):
    from backend.core.download_registry import registry

    class FakeResp:
        status_code = 200
        async def aiter_bytes(self, _n):
            yield b"x" * 10
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, method, url, headers=None): return FakeResp()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dl.httpx, "AsyncClient", FakeClient)
    target = tmp_path / "sub" / "01.mp4"
    item = dl._Item(fid="1", name="01.mp4", size=10, local_path=target)
    jid = registry.create(task_id=None, taskname="T", filename="01.mp4", dest_path=str(target), total=10)
    ok, msg = await dl._fetch_one({"download_url": "http://dl/1", "size": 10}, item, "C=1", "UA", job_id=jid)
    monkeypatch.undo()
    job = next(j for j in registry.snapshot() if j["id"] == jid)
    assert ok and job["status"] == "done" and job["done"] == 10


@pytest.mark.asyncio
async def test_fetch_one_reports_skipped_and_failed(tmp_path):
    from backend.core.download_registry import registry

    # 已存在同大小 → skipped
    target = tmp_path / "s.mp4"
    target.write_bytes(b"y" * 10)
    item = dl._Item(fid="s", name="s.mp4", size=10, local_path=target)
    jid = registry.create(task_id=None, taskname="T", filename="s.mp4", dest_path=str(target), total=10)
    ok, msg = await dl._fetch_one({"download_url": "http://dl", "size": 10}, item, "", "UA", job_id=jid)
    assert ok and "跳过" in msg
    assert next(j for j in registry.snapshot() if j["id"] == jid)["status"] == "skipped"

    # HTTP 非 200 → failed
    class BadResp:
        status_code = 403
        async def aiter_bytes(self, _n): yield b""
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    class BadClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, method, url, headers=None): return BadResp()
    mp = pytest.MonkeyPatch(); mp.setattr(dl.httpx, "AsyncClient", BadClient)
    item2 = dl._Item(fid="b", name="b.mp4", size=5, local_path=tmp_path / "b.mp4")
    jid2 = registry.create(task_id=None, taskname="T", filename="b.mp4", dest_path=str(item2.local_path), total=5)
    ok2, _ = await dl._fetch_one({"download_url": "http://dl", "size": 5}, item2, "", "UA", job_id=jid2)
    mp.undo()
    job2 = next(j for j in registry.snapshot() if j["id"] == jid2)
    assert not ok2 and job2["status"] == "failed" and "403" in job2["error"]
```

- [ ] **Step 2: 运行验证失败**

Run: `.venv/bin/pytest -q backend/tests/test_download.py -k "reports_progress or reports_skipped"`
Expected: FAIL — `_fetch_one() got unexpected keyword 'job_id'`

- [ ] **Step 3: 实现插桩**

在 `download_service.py` 顶部加 `import time` 与 `from ..core.download_registry import registry`。

`download_task_files` 增参数并透传（仅内置分支需要）：

```python
async def download_task_files(
    driver, saved, cfg, *, download_subdir=False, savepath_override="", log=None,
    task_id: int | None = None, taskname: str = "",
):
    ...
    if cfg.mode == "aria2":
        lines = await _aria2_submit(driver, items, cfg, log)
    else:
        lines = await _builtin_download(driver, items, cfg, log, task_id=task_id, taskname=taskname)
    ...
```

`_builtin_download` 为每个文件建 job：

```python
async def _builtin_download(driver, items, cfg, log, *, task_id=None, taskname=""):
    by_fid, cookie_str = await _resolve_links(driver, items)
    ua = getattr(driver, "UA", "Mozilla/5.0")
    sem = asyncio.Semaphore(cfg.concurrency)

    async def one(item):
        row = by_fid.get(item.fid)
        if not row:
            return f"❌ 取直链失败: {item.name}"
        job_id = registry.create(
            task_id=task_id, taskname=taskname, filename=item.name,
            dest_path=str(item.local_path), total=int(row.get("size") or item.size or 0),
        )
        async with sem:
            try:
                ok, msg = await _fetch_one(row, item, cookie_str, ua, job_id=job_id)
            except Exception as exc:  # noqa: BLE001
                registry.update(job_id, status="failed", error=str(exc))
                ok, msg = False, f"{item.name}: {exc}"
        log("info" if ok else "warn", f"📥 {msg}")
        return f"{'✅' if ok else '❌'} {msg}"

    return list(await asyncio.gather(*(one(i) for i in items)))
```

`_fetch_one` 增 `job_id` 并在写块时更新（保留原返回契约）：

```python
async def _fetch_one(row, item, cookie_str, ua, *, job_id=None):
    path = item.local_path
    path.parent.mkdir(parents=True, exist_ok=True)
    size = int(row.get("size") or item.size or 0)
    if path.exists() and size and path.stat().st_size == size:
        if job_id:
            registry.update(job_id, status="skipped")
        return True, f"跳过（已存在）{item.name}"
    part = path.with_name(path.name + ".part")
    headers = {"user-agent": ua}
    if cookie_str:
        headers["cookie"] = cookie_str
    started = time.monotonic()
    last_tick = started
    written = 0
    async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
        async with client.stream("GET", row["download_url"], headers=headers) as resp:
            if resp.status_code != 200:
                if job_id:
                    registry.update(job_id, status="failed", error=f"HTTP {resp.status_code}")
                return False, f"{item.name}: HTTP {resp.status_code}"
            with part.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1 << 16):
                    fh.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if job_id and now - last_tick >= 0.25:
                        last_tick = now
                        speed = written / max(now - started, 1e-6)
                        registry.update(job_id, done=written, speed=speed, status="downloading")
    if size and written != size:
        part.unlink(missing_ok=True)
        if job_id:
            registry.update(job_id, status="failed", error=f"大小不符 {written}/{size}")
        return False, f"{item.name}: 大小不符 {written}/{size}"
    os.replace(part, path)
    if job_id:
        registry.update(job_id, done=written, total=written or size, status="done")
    return True, f"{item.name}（{written / 1024 / 1024:.1f}MB）"
```

`task_service.py:176` 调用处补两参：

```python
        lines = await download_task_files(
            driver,
            result.files,
            cfg,
            download_subdir=bool(getattr(task, "download_subdir", False)),
            savepath_override=getattr(task, "download_savepath", "") or "",
            log=tlog,
            task_id=task.id,
            taskname=task.taskname,
        )
```

- [ ] **Step 4: 运行验证通过（含既有下载测试不回归）**

Run: `.venv/bin/pytest -q backend/tests/test_download.py`
Expected: PASS（新增 2 + 原有用例全绿）

- [ ] **Step 5: 提交**

```bash
git add backend/services/download_service.py backend/services/task_service.py backend/tests/test_download.py
git commit -m "feat(download): 内置下载器上报 done/speed/status 到注册表"
```

---

### Task 4: 前端「下载」视图（类型 + 客户端 + 视图 + 路由 + 导航）

**Files:**
- Modify: `frontend/src/api/types.ts`（新增 `DownloadJob`）
- Modify: `frontend/src/api/client.ts`（新增 `listDownloads`）
- Create: `frontend/src/views/DownloadsView.vue`
- Modify: `frontend/src/router.ts`（新增 `/downloads`）
- Modify: `frontend/src/App.vue`（nav 数组新增一项）

**Interfaces:**
- Consumes: Task 2 的 `GET /api/downloads` → `{jobs: DownloadJob[]}`。
- Produces: 路由 `/downloads` 渲染 `DownloadsView`，1.5s 轮询。

- [ ] **Step 1: types.ts 增类型**

```typescript
// 追加到 frontend/src/api/types.ts
export interface DownloadJob {
  id: string;
  task_id: number | null;
  taskname: string;
  filename: string;
  dest_path: string;
  total: number;
  done: number;
  speed: number;
  status: "queued" | "downloading" | "done" | "failed" | "skipped";
  error: string;
  started_at: number;
  updated_at: number;
}
```

- [ ] **Step 2: client.ts 增方法**

在 `export const api = { ... }` 的「文件」区块后加：

```typescript
  // 下载进度
  listDownloads: () => request<{ jobs: DownloadJob[] }>("/api/downloads"),
```

并在 `client.ts` 顶部 `import type { ... }` 列表加入 `DownloadJob`。

- [ ] **Step 3: 新建 DownloadsView.vue**

```vue
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { api } from "../api/client";
import type { DownloadJob } from "../api/types";

const jobs = ref<DownloadJob[]>([]);
const error = ref("");
let timer: number | undefined;

function pct(j: DownloadJob): number {
  if (!j.total) return j.status === "done" ? 100 : 0;
  return Math.min(100, Math.round((j.done / j.total) * 100));
}
function fmtSize(n: number): string {
  if (n <= 0) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}
function progressStatus(s: string): "" | "success" | "exception" | "warning" {
  if (s === "done" || s === "skipped") return "success";
  if (s === "failed") return "exception";
  return "";
}
function statusText(s: string): string {
  return ({ queued: "排队", downloading: "下载中", done: "完成", failed: "失败", skipped: "跳过" } as Record<string, string>)[s] || s;
}

async function refresh() {
  if (document.hidden) return;
  try {
    const r = await api.listDownloads();
    jobs.value = r.jobs;
    error.value = "";
  } catch (e) {
    error.value = (e as Error).message;
  }
}

onMounted(() => {
  refresh();
  timer = window.setInterval(refresh, 1500);
});
onBeforeUnmount(() => window.clearInterval(timer));
</script>

<template>
  <div>
    <div class="sticky-bar">
      <span class="sticky-bar__title">下载任务</span>
      <span v-if="error" class="err">{{ error }}</span>
    </div>
    <el-table :data="jobs" empty-text="暂无下载记录" row-key="id">
      <el-table-column prop="taskname" label="任务" min-width="120" show-overflow-tooltip />
      <el-table-column prop="filename" label="文件" min-width="200" show-overflow-tooltip />
      <el-table-column label="进度" min-width="200">
        <template #default="{ row }">
          <el-progress :percentage="pct(row)" :status="progressStatus(row.status)" :stroke-width="12" />
          <span class="sub">{{ fmtSize(row.done) }} / {{ row.total ? fmtSize(row.total) : "未知" }}</span>
        </template>
      </el-table-column>
      <el-table-column label="速度" width="110">
        <template #default="{ row }">{{ row.status === "downloading" ? `${fmtSize(row.speed)}/s` : "-" }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="row.status === 'failed' ? 'danger' : row.status === 'done' ? 'success' : 'info'" size="small">
            {{ statusText(row.status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="dest_path" label="目标路径" min-width="220" show-overflow-tooltip />
    </el-table>
  </div>
</template>

<style scoped>
.err { color: var(--danger); font-size: 13px; margin-left: 12px; }
.sub { font-size: 12px; color: var(--text-muted); }
</style>
```

- [ ] **Step 4: router.ts 增路由**

在 `routes` 数组内加：

```typescript
    { path: "/downloads", component: () => import("./views/DownloadsView.vue") },
```

- [ ] **Step 5: App.vue 增导航项**

在 `nav` 数组「日志」前加：

```typescript
  { to: "/downloads", label: "下载", icon: "M12 4v12m0 0l-4-4m4 4l4-4M4 20h16" },
```

- [ ] **Step 6: 构建校验**

Run: `cd frontend && npm run build`
Expected: 构建成功（tsc 无类型错误）。

- [ ] **Step 7: 提交**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts frontend/src/views/DownloadsView.vue frontend/src/router.ts frontend/src/App.vue
git commit -m "feat(ui): 新增下载进度视图 /downloads"
```

---

### Task 5: 端到端真机验证

**Files:** 无（验证任务）。

**Interfaces:** Consumes: 全部前序任务。

- [ ] **Step 1: 起后端 + 前端**

```bash
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8432   # 后台
cd frontend && npm run dev   # vite，代理 /api → 8432
```

- [ ] **Step 2: 触发一次可观察进度的下载**

对某有效分享建 `auto_download` 任务，pattern 匹配数百 KB 文件（如 `backdrop\.jpg`），立即运行；
0.1MB/s 限速下进度条应可见移动。

- [ ] **Step 3: 浏览器验证 `/downloads`**

打开下载视图，确认：下载中行百分比/速度实时刷新 → 完成转绿；截图留证。
确认失败/跳过态显示正确（可临时用坏链接复现 failed）。

- [ ] **Step 4: 记录结果**

在 PR/提交说明里附验证结论与截图路径。

---

## Self-Review（本计划对照 spec）

- **Spec 覆盖**：§4.1 注册表→Task1；§4.3 接口→Task2；§4.2 插桩→Task3；§4.4 前端→Task4；§7 测试→各任务 Step1 + Task5；§6 边界（total=0 未知大小、并发、失败不中断、有界历史）分别在 Task1(有界)、Task3(failed/skipped)、Task4(未知大小文案)覆盖。
- **占位符**：无 TBD/TODO；每个代码步给出可编译/可运行的完整片段。
- **类型一致**：`registry.create/update/snapshot`、`DownloadJob` 字段、`_fetch_one(..., job_id=)`、`download_task_files(..., task_id, taskname)` 在 Task1→3→4 间签名一致；前端 `DownloadJob` 与后端 `asdict` 字段逐一对应。
- **非目标**：aria2/控制/持久化未出现，符合 spec §2。
