# 下载历史账本与失败重下 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把"下载到本地"从重启即清的内存快照升级为 SQLite 账本，提供进行中/历史双 tab、aria2 终态回填、文件到位校验与单文件重下。

**Architecture:** 新增 `download_record` 表与 `services/download_history.py` 作为账本唯一出入口（`start()` 落 `queued`、`finish()` 写终态）；`core/download_registry.py` 保持纯内存只管实时进度；`download_service` 抽出 `download_items()` 使重下能按已知目标路径复用整条下载链路；非终态记录由 `reconcile()` 在查询时收口（aria2 RPC 优先 → 文件 stat 兜底 → 24h 判失败）。

**Tech Stack:** FastAPI + SQLModel/SQLite + APScheduler(AsyncIOScheduler) + httpx；Vue 3 `<script setup>` + Element Plus + Pinia；pytest(asyncio_mode=auto) + ruff。

**Spec:** `docs/superpowers/specs/2026-10-04-download-history-design.md`

## Global Constraints

- Python `requires-python >= 3.11`；ruff `line-length = 110`，`select = ["E","F","W","I","UP","B"]`，`ignore = ["E501"]`。
- **不新增任何运行时依赖**（账本用已有 sqlmodel，清理调度用已有 apscheduler）。
- 用户可见文案、日志、注释一律中文（沿用现有风格，如"⚠️ aria2 不可达，自动改用内置下载器"）。
- **转存段全局锁语义不动**：下载仍在 `_run_lock` 之外执行（`task_service.py:161-178`），重下同样只走下载段、不触发转存。
- 测试命令：`python3 -m pytest backend/tests -q`（`testpaths = ["backend/tests"]`，`asyncio_mode = "auto"`，无需 `@pytest.mark.asyncio`）。
- `backend/tests/conftest.py` 已把 `DATA_DIR` 指向临时目录，**任何测试都不得读写真实 `data/xiao_pan.db`**（历史事故：修复前的测试跑法毁掉过真实任务）。
- 前端类型检查：`cd frontend && npm run typecheck`；构建：`npm run build`。
- 每个 task 一次提交，中文 conventional commit（`feat(download): ...` / `fix(...)`）；**不 push**，由用户手动推。
- 账本写入失败绝不影响下载主流程（全程 `try/except` + `log("warn", ...)`）。

---

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `backend/models.py` | 新增 `DownloadRecord` 表（账本数据形状） |
| `backend/core/download_registry.py` | 纯内存实时进度；新增 `get()`，`snapshot()` 只返回进行中 |
| `backend/services/download_history.py` | **新增**，账本唯一出入口：`start` / `finish` / `list_records` / `open_records` / `reconcile` / `prune` / `file_state` / `get_record` |
| `backend/services/download_service.py` | 下载执行；`_Item`→`DownloadItem`，抽 `download_items()`，接账本写入点，加 `retry_record()` |
| `backend/api/routes_downloads.py` | HTTP 层：进行中 + 历史 + retry + prune；历史路由必须**先于** `/{job_id}` 路由声明 |
| `backend/api/deps.py` | `DEFAULT_SETTINGS["download"]` 加 `history_retention` |
| `backend/services/task_service.py` | 把 `account_id` 传进下载（账本要能重下） |
| `backend/core/scheduler.py` | 加通用 `add_daily()` |
| `backend/main.py` | `_prune_job()` + 启动时清理一次 + 每天 04:00 |
| `frontend/src/api/types.ts` / `client.ts` | `DownloadRecord` 类型与 4 个新接口 |
| `frontend/src/views/DownloadsView.vue` | 进行中 / 历史 双 tab |
| `frontend/src/components/settings/SettingsDownload.vue` | 保留策略下拉 |
| `README.md` | 补下载历史与重下说明 |

---

## Task 1: 数据表与基础类型

**Files:**
- Modify: `backend/models.py`（在 `Setting` 之前追加）
- Modify: `backend/core/download_registry.py:33-101`
- Modify: `backend/services/download_service.py:57-62,85-111,166-195,310-312`（`_Item` 改名）
- Test: `backend/tests/test_download_registry.py`（追加）

**Interfaces:**
- Consumes: 无
- Produces:
  - `DownloadRecord`（SQLModel 表，字段见下）
  - `DownloadRegistry.get(job_id: str) -> DownloadJob | None`
  - `DownloadItem(fid: str, name: str, size: int, local_path: Path)`（原 `_Item` 公开化）

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_download_registry.py` 末尾（文件顶部需补 `from pathlib import Path`）：

```python
def test_registry_get_reads_active_and_terminal():
    r = DownloadRegistry(history=5)
    a = r.create(task_id=1, taskname="T", filename="a.mp4", dest_path="/d/a.mp4", total=10)
    assert r.get(a).status == "queued"
    r.update(a, done=10, status="done")
    assert r.get(a).status == "done"  # 进 _done 后仍能取出，供落库
    assert r.get("nope") is None


def test_download_record_table_defaults():
    from sqlmodel import select

    from backend.database import session_scope
    from backend.models import DownloadRecord

    with session_scope() as s:
        s.add(DownloadRecord(source="builtin", ref_id="r-t1", taskname="T", filename="a.mp4", dest_path="/d/a.mp4"))
    with session_scope() as s:
        row = s.exec(select(DownloadRecord).where(DownloadRecord.ref_id == "r-t1")).first()
    assert row.status == "queued" and row.size_total == 0 and row.finished_at is None
    assert row.task_id is None and row.account_id is None


def test_download_item_is_public():
    from backend.services.download_service import DownloadItem

    i = DownloadItem(fid="f1", name="a.mp4", size=10, local_path=Path("/d/a.mp4"))
    assert i.fid == "f1" and i.size == 10 and i.local_path.name == "a.mp4"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest backend/tests/test_download_registry.py -q`
Expected: FAIL —`DownloadRegistry.get` 不存在（AttributeError）、`DownloadRecord` ImportError、`DownloadItem` ImportError。

- [ ] **Step 3: 加 `DownloadRecord` 表**

`backend/models.py`，插在 `class Setting` 之前：

```python
class DownloadRecord(SQLModel, table=True):
    """下载账本：一条 = 一次下载动作的完整生命周期（start 落 queued，finish 写终态）。"""

    __tablename__ = "download_record"

    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(default="builtin", index=True)  # builtin | aria2
    ref_id: str = Field(default="", index=True)  # 内置=registry job_id；aria2=gid
    task_id: int | None = None  # 只存值不加外键：任务删除后历史仍要留
    taskname: str = ""
    filename: str = ""
    dest_path: str = ""
    size_total: int = 0
    size_done: int = 0
    fid: str = ""  # 网盘 fid，重下取直链用
    driver_key: str = ""
    account_id: int | None = None
    status: str = Field(default="queued", index=True)  # queued|downloading|done|failed|skipped|stopped
    error: str = ""
    created_at: NaiveDatetime = Field(default_factory=_now)
    finished_at: NaiveDatetime | None = None
```

表由 `database.init_db()` 的 `create_all` + `_auto_add_columns()` 自动建立，**不写迁移脚本**。

- [ ] **Step 4: 加 `DownloadRegistry.get()`**

`backend/core/download_registry.py`，插在 `cancel_requested` 之前：

```python
    def get(self, job_id: str) -> DownloadJob | None:
        """按 id 取 job（含已进 _done 的终态），供服务层落账本。"""
        job = self._active.get(job_id)
        if job is not None:
            return job
        return next((j for j in self._done if j.id == job_id), None)
```

本 Task **不动 `snapshot()`**（去掉历史部分是 Task 3 的事）。

- [ ] **Step 5: `_Item` 提为公开 `DownloadItem`**

`backend/services/download_service.py` 全文替换 5 处：

```python
@dataclass
class DownloadItem:
    fid: str
    name: str
    size: int
    local_path: Path
```

引用点同步改名：`collect_files`（返回类型 + 两处构造）、`_walk_dir`、`_resolve_links(driver: CloudDrive, items: list[DownloadItem])`、`_builtin_download` 的 `one(item: DownloadItem)`、`_fetch_one(row: dict, item: DownloadItem, ...)`、`_aria2_submit(..., items: list[DownloadItem], ...)`。用 `sed` 或编辑器全局替换 `_Item` → `DownloadItem` 后检查 docstring 未残留。

- [ ] **Step 6: 跑测试确认通过**

Run: `python3 -m pytest backend/tests -q`
Expected: 全绿（既有用例不应因改名而失败——它们只通过 `collect_files` 的返回值访问字段）。

- [ ] **Step 7: 静态检查**

Run: `python3 -m ruff check backend`
Expected: `All checks passed!`

- [ ] **Step 8: 提交**

```bash
git add backend/models.py backend/core/download_registry.py backend/services/download_service.py backend/tests/test_download_registry.py
git commit -m "feat(download): 加下载账本表与公开 DownloadItem 类型，registry 支持按 id 取终态"
```

---

## Task 2: 账本服务与写入点

**Files:**
- Create: `backend/services/download_history.py`
- Modify: `backend/services/download_service.py:114-195`（`download_task_files` 加身份参数、两个写入点）
- Modify: `backend/services/task_service.py:178`（传 `account_id`）
- Test: `backend/tests/test_download_history.py`（新建）

**Interfaces:**
- Consumes: `DownloadRecord`（Task 1）、`registry.get()`（Task 1）、`DownloadItem`（Task 1）
- Produces:
  - `download_history.TERMINAL: set[str] = {"done","failed","skipped","stopped"}`
  - `download_history.start(*, source, ref_id, task_id, taskname, filename, dest_path, size_total, fid, driver_key, account_id) -> int`
  - `download_history.finish(ref_id: str, *, source: str, status: str, size_done: int | None = None, size_total: int | None = None, error: str = "") -> None`
  - `download_service.download_task_files(driver, saved, cfg, *, download_subdir=False, savepath_override="", log=None, task_id=None, taskname="", account_id=None)`（新增 `account_id`）

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_download_history.py`：

```python
"""下载账本测试：生命周期写入、对账收口、文件校验、清理。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import select

from backend.core.download_registry import DownloadRegistry, registry  # noqa: F401
from backend.database import session_scope
from backend.models import DownloadRecord
from backend.services import download_history as hist
from backend.services import download_service as dl
from backend.tests.test_download import DlDriver, cfg, saved


def _rows(task_id: int | None = None) -> list[DownloadRecord]:
    """按 task_id 取本用例自己的记录，避免依赖测试执行顺序。"""
    with session_scope() as s:
        stmt = select(DownloadRecord).order_by(DownloadRecord.id)
        if task_id is not None:
            stmt = stmt.where(DownloadRecord.task_id == task_id)
        return list(s.exec(stmt).all())


async def test_builtin_flow_writes_one_terminal_record(tmp_path, monkeypatch):
    async def fake_fetch(row, item, cookie_str, ua, *, job_id=None):
        registry.update(job_id, done=row["size"], status="done")
        return True, f"{item.name}（0.0MB）"

    monkeypatch.setattr(dl, "_fetch_one", fake_fetch)
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())

    await dl.download_task_files(
        DlDriver(), [saved("1")], cfg(tmp_path), task_id=701, taskname="追更", account_id=3, driver_key="fake"
    )

    rows = _rows(701)
    assert len(rows) == 1  # start + finish 是同一条，不是两条
    r = rows[0]
    assert (r.status, r.source, r.taskname, r.fid, r.account_id, r.driver_key) == (
        "done", "builtin", "追更", "1", 3, "fake",
    )
    assert r.size_total == 10 and r.size_done == 10 and r.finished_at is not None
    assert r.dest_path.endswith("动漫/剧/01.mp4")
    registry.remove(r.ref_id)


async def test_builtin_failure_records_error(tmp_path, monkeypatch):
    async def boom(row, item, cookie_str, ua, *, job_id=None):
        raise RuntimeError("直链 403")

    monkeypatch.setattr(dl, "_fetch_one", boom)
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())
    await dl.download_task_files(DlDriver(), [saved("x")], cfg(tmp_path), task_id=702, taskname="t")
    r = _rows(702)[-1]
    assert r.status == "failed" and "直链 403" in r.error


async def test_history_write_failure_does_not_break_download(tmp_path, monkeypatch):
    def blow_up(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(hist, "start", blow_up)
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())

    async def fake_fetch(row, item, cookie_str, ua, *, job_id=None):
        registry.update(job_id, status="done")
        return True, "ok"

    monkeypatch.setattr(dl, "_fetch_one", fake_fetch)
    lines = await dl.download_task_files(DlDriver(), [saved("1")], cfg(tmp_path), task_id=703)
    assert lines and lines[0].startswith("✅")


async def test_aria2_submit_records_queued_with_gid(tmp_path, monkeypatch):
    class FakeResp:
        def json(self):
            return {"result": "GID-777"}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            return FakeResp()

    monkeypatch.setattr(dl.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(dl, "aria2_reachable", lambda c: _ret(True))
    c = cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800")
    await dl.download_task_files(
        DlDriver(), [saved("1")], c, task_id=704, taskname="A", account_id=5, driver_key="fake"
    )

    r = _rows(704)[-1]
    assert (r.status, r.source, r.ref_id, r.taskname) == ("queued", "aria2", "GID-777", "A")


async def test_start_then_finish_updates_single_row():
    rid = hist.start(
        source="builtin", ref_id="r-2", task_id=705, taskname="t", filename="f", dest_path="/d/f",
        size_total=100, fid="F", driver_key="fake", account_id=None,
    )
    hist.finish("r-2", source="builtin", status="skipped", size_done=0, size_total=100, error="已存在")
    rows = _rows(705)
    assert len(rows) == 1 and rows[0].status == "skipped" and rows[0].error == "已存在"
    assert rows[0].finished_at is not None


async def test_finish_unknown_ref_is_noop():
    hist.finish("no-such-ref", source="aria2", status="done")  # 不抛异常
    assert _rows(799) == []


async def _noop():
    return None


async def _ret(v):
    return v
```

`_rows(task_id)` 按任务隔离取数——测试共用同一个临时库，**任何断言都不得依赖全表长度或执行顺序**。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest backend/tests/test_download_history.py -q`
Expected: FAIL —`ImportError: cannot import name 'download_history'`。

- [ ] **Step 3: 实现 `download_history.start/finish`**

新建 `backend/services/download_history.py`：

```python
"""下载账本：把每个下载动作的完整生命周期落到 SQLite，供历史查询、到位校验与失败重下。

写入只有两个点：start() 落 queued，finish() 写终态。实时进度（速度/已下字节）仍只在
内存 registry，不进这张表 —— 避免每 0.25s 一次写盘。
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import select

from ..database import session_scope
from ..models import DownloadRecord

TERMINAL = {"done", "failed", "skipped", "stopped"}


def start(
    *,
    source: str,
    ref_id: str,
    task_id: int | None,
    taskname: str,
    filename: str,
    dest_path: str,
    size_total: int,
    fid: str,
    driver_key: str,
    account_id: int | None,
) -> int:
    row = DownloadRecord(
        source=source,
        ref_id=ref_id,
        task_id=task_id,
        taskname=taskname,
        filename=filename,
        dest_path=dest_path,
        size_total=int(size_total or 0),
        fid=fid,
        driver_key=driver_key,
        account_id=account_id,
        status="queued",
    )
    with session_scope() as session:
        session.add(row)
        session.flush()
        return int(row.id)


def finish(
    ref_id: str,
    *,
    source: str,
    status: str,
    size_done: int | None = None,
    size_total: int | None = None,
    error: str = "",
) -> None:
    """按 ref_id 定位最近一条，写终态。找不到就静默返回（记录可能已被清理）。"""
    with session_scope() as session:
        row = session.exec(
            select(DownloadRecord)
            .where(DownloadRecord.ref_id == ref_id, DownloadRecord.source == source)
            .order_by(DownloadRecord.id.desc())
        ).first()
        if row is None:
            return
        row.status = status
        if size_done is not None:
            row.size_done = int(size_done)
        if size_total is not None:
            row.size_total = int(size_total)
        row.error = error or ""
        row.finished_at = datetime.now()
        session.add(row)
```

顶部 import 就是上面这四行（`asyncio` / `os` / `stat` / `func` 等在后续 Task 分别补）。

- [ ] **Step 4: 接下载服务的写入点**

`backend/services/download_service.py` 顶部 import 加 `from . import download_history as history`。

在 `safe_name` 之后加两个旁路写入助手（永不抛）：

```python
def _history_start(log, *, source: str, ref_id: str, item: DownloadItem, size: int, task_id, taskname,
                   account_id, driver_key: str) -> None:
    try:
        history.start(
            source=source, ref_id=ref_id, task_id=task_id, taskname=taskname, filename=item.name,
            dest_path=str(item.local_path), size_total=size, fid=item.fid,
            driver_key=driver_key, account_id=account_id,
        )
    except Exception as exc:  # noqa: BLE001 账本是旁路观测，绝不中断下载
        log("warn", f"下载账本写入失败（不影响下载）：{exc}")


def _history_finish(log, *, source: str, ref_id: str, ok: bool, fallback_name: str) -> None:
    try:
        job = registry.get(ref_id)
        status = job.status if job and job.status in history.TERMINAL else ("done" if ok else "failed")
        done = job.done if job else 0
        total = job.total if job else 0
        error = job.error if job else ("" if ok else fallback_name)
        history.finish(ref_id, source=source, status=status, size_done=done, size_total=total, error=error)
    except Exception as exc:  # noqa: BLE001
        log("warn", f"下载账本终态写入失败（不影响下载）：{exc}")
```

`download_task_files` 签名加 `account_id: int | None = None`，并向 `_builtin_download` / `_aria2_submit` 透传 `account_id` 与 `driver_key=driver.key`。

`_builtin_download` 的 `one()` 改成：

```python
    async def one(item: DownloadItem) -> str:
        row = by_fid.get(item.fid)
        if not row:
            return f"❌ 取直链失败: {item.name}"
        size = int(row.get("size") or item.size or 0)
        job_id = registry.create(
            task_id=task_id, taskname=taskname, filename=item.name,
            dest_path=str(item.local_path), total=size,
        )
        _history_start(log, source="builtin", ref_id=job_id, item=item, size=size, task_id=task_id,
                       taskname=taskname, account_id=account_id, driver_key=driver_key)
        async with sem:
            try:
                ok, msg = await _fetch_one(row, item, cookie_str, ua, job_id=job_id)
            except Exception as exc:  # noqa: BLE001
                registry.update(job_id, status="failed", error=str(exc))
                ok, msg = False, f"{item.name}: {exc}"
        _history_finish(log, source="builtin", ref_id=job_id, ok=ok, fallback_name=msg)
        log("info" if ok else "warn", f"📥 {msg}")
        return f"{'✅' if ok else '❌'} {msg}"
```

`_aria2_submit` 签名补 `*, task_id=None, taskname="", account_id=None, driver_key=""`，成功分支里投递拿到 gid 后落账本：

```python
            gid = result.get("result")
            if gid:
                log("info", f"📥 aria2 已投递 {item.name}")
                _history_start(log, source="aria2", ref_id=str(gid), item=item,
                               size=int(row.get("size") or item.size or 0), task_id=task_id,
                               taskname=taskname, account_id=account_id, driver_key=driver_key)
                lines.append(f"✅ aria2 已投递 {item.name}")
            else:
                lines.append(f"❌ aria2 {item.name}: {result.get('error')}")
```

（替换原先只判 `result.get("result")` 真假的写法；`addUri` 返回的 `result` 即 gid 字符串。）

- [ ] **Step 5: `task_service` 传账号**

`backend/services/task_service.py:178` 与 `_download_for_task`：

```python
                await _download_for_task(driver, task, result, settings, notify_lines, tlog, account_id=account.id)
```

```python
async def _download_for_task(driver, task, result, settings, notify_lines, tlog, *, account_id=None) -> None:
```

并在其内部 `download_task_files(...)` 调用里加 `account_id=account_id`。

- [ ] **Step 6: 跑测试确认通过**

Run: `python3 -m pytest backend/tests/test_download_history.py backend/tests/test_download.py -q`
Expected: 全绿。

- [ ] **Step 7: 全量测试 + ruff**

Run: `python3 -m pytest backend/tests -q && python3 -m ruff check backend`
Expected: 全绿；`All checks passed!`

- [ ] **Step 8: 提交**

```bash
git add backend/services/download_history.py backend/services/download_service.py backend/services/task_service.py backend/tests/test_download_history.py
git commit -m "feat(download): 下载账本落库，内置与 aria2 两条源都走 start/finish 生命周期"
```

---

## Task 3: 历史查询端点与进行中语义收窄

**Files:**
- Modify: `backend/core/download_registry.py:98-100`
- Modify: `backend/services/download_history.py`（加 `file_state` / `list_records` / `get_record`）
- Modify: `backend/api/routes_downloads.py`
- Test: `backend/tests/test_download_history.py`、`backend/tests/test_download_api.py`、`backend/tests/test_download_registry.py`（改既有断言）

**Interfaces:**
- Consumes: `DownloadRecord`、`history.start/finish`（Task 2）
- Produces:
  - `download_history.file_state(path: str) -> str`（`ok`/`missing`/`unknown`）
  - `download_history.list_records(*, page=1, page_size=50, status="", task_id=None, keyword="") -> dict`（`{"items": [...], "total": int}`，item 为 dict，无 `file_state` 字段）
  - `download_history.get_record(record_id: int) -> dict | None`
  - `GET /api/downloads/history`
  - `DownloadRegistry.snapshot()` **只返回进行中**

- [ ] **Step 1: 改 registry 快照断言并加 list_records 测试**

`backend/tests/test_download_registry.py`：把 `test_create_update_snapshot_lifecycle` 中"进行中(b)排在历史(a)之前"那段替换为——

```python
    r.update(a, done=100, status="done")
    snap = r.snapshot()
    ids = [j["id"] for j in snap]
    # 快照只含进行中：终态记录交给下载账本
    assert ids == [b]
    assert r.get(a).status == "done"
```

`test_history_is_bounded` 改为按 `_done` 容量断言（不再经 snapshot 观察）：

```python
def test_history_is_bounded():
    r = DownloadRegistry(history=2)
    ids = [r.create(task_id=None, taskname="T", filename=f"{i}", dest_path="/d", total=1) for i in range(5)]
    for i in ids:
        r.update(i, status="done")
    assert r.snapshot() == []  # 全部终态，进行中为空
    assert [j.id for j in r._done] == ids[-2:]  # 只保留最近 2 条
    assert r.get(ids[-1]).status == "done" and r.get(ids[0]) is None
```

`backend/tests/test_download_history.py` 追加：

```python
def _seed(n: int, *, status: str = "done", task_id: int, filename: str = "a.mp4", dest: str = "/d/a.mp4") -> None:
    ref_base = f"{task_id}-{status}"
    for i in range(n):
        ref = f"{ref_base}-{i}"
        hist.start(
            source="builtin", ref_id=ref, task_id=task_id, taskname="T", filename=filename,
            dest_path=dest, size_total=10, fid=f"F{i}", driver_key="fake", account_id=None,
        )
        if status != "queued":
            hist.finish(ref, source="builtin", status=status, size_done=10)


def test_list_records_filters_and_pages():
    _seed(3, status="done", task_id=801, filename="英雄.mp4")
    _seed(2, status="failed", task_id=802, filename="反派.mkv")

    r = hist.list_records(task_id=801, page=1, page_size=2)
    assert r["total"] == 3 and len(r["items"]) == 2
    r2 = hist.list_records(task_id=801, page=2, page_size=2)
    assert r2["total"] == 3 and len(r2["items"]) == 1
    assert hist.list_records(status="failed", task_id=802)["total"] == 2
    assert hist.list_records(keyword="英雄")["total"] == 3
    assert hist.list_records(keyword="绝无此名")["total"] == 0
    assert all(i["status"] in ("failed", "queued") for i in hist.list_records(status="failed,queued")["items"])


def test_file_state_ok_missing_and_directory(tmp_path):
    good = tmp_path / "in.mp4"
    good.write_bytes(b"x")
    assert hist.file_state(str(good)) == "ok"
    assert hist.file_state(str(tmp_path / "gone.mp4")) == "missing"
    d = tmp_path / "some_dir"
    d.mkdir()
    assert hist.file_state(str(d)) == "unknown"  # 目录不是常规文件


def test_file_state_unknown_on_permission_error(monkeypatch):
    import types

    def boom(path):
        raise PermissionError("挂载抖动")

    # 只替换模块命名空间里的 os（file_state 只用到 os.stat），不碰全局 os
    monkeypatch.setattr(hist, "os", types.SimpleNamespace(stat=boom))
    assert hist.file_state("/anywhere") == "unknown"


def test_get_record_roundtrip():
    _seed(1, status="failed", task_id=803, filename="z.mp4")
    rec = hist.get_record(_id_of_ref("803-failed-0"))
    assert rec["task_id"] == 803 and rec["status"] == "failed" and rec["fid"].startswith("F")
    assert hist.get_record(0) is None


def _id_of_ref(ref: str) -> int:
    with session_scope() as s:
        return int(s.exec(select(DownloadRecord).where(DownloadRecord.ref_id == ref)).first().id)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest backend/tests/test_download_history.py backend/tests/test_download_registry.py -q`
Expected: FAIL —`AttributeError: module ... has no attribute 'list_records'`、snapshot 断言仍含终态。

- [ ] **Step 3: `snapshot()` 只返回进行中**

`backend/core/download_registry.py:98-100`：

```python
    def snapshot(self) -> list[dict]:
        """只返回进行中；终态记录由下载账本负责展示。"""
        return [{**asdict(j), "source": "builtin"} for j in sorted(self._active.values(), key=lambda j: j.started_at)]
```

同时把模块 docstring 改为「下载进度内存注册表：内置下载器写盘时更新，前端轮询快照；终态历史见 `services/download_history`。进程重启即清空。」

- [ ] **Step 4: `file_state` / `list_records` / `get_record`**

追加到 `backend/services/download_history.py`（顶部补 `import os, stat`、`from sqlalchemy import func, or_`、`from sqlmodel import col`）：

```python
def file_state(path: str) -> str:
    """文件到位校验：ok=常规文件在；missing=确实不存在；unknown=读不到（权限/挂载异常），不当成丢失。"""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    return "ok" if stat.S_ISREG(st.st_mode) else "unknown"


def _conditions(*, status: str, task_id: int | None, keyword: str) -> list:
    conds = []
    wanted = [s for s in (status or "").split(",") if s]
    if wanted:
        conds.append(DownloadRecord.status.in_(wanted))
    if task_id:
        conds.append(DownloadRecord.task_id == int(task_id))
    if keyword:
        like = f"%{keyword}%"
        conds.append(or_(DownloadRecord.filename.like(like), DownloadRecord.dest_path.like(like)))
    return conds


def list_records(*, page: int = 1, page_size: int = 50, status: str = "", task_id: int | None = None,
                 keyword: str = "") -> dict:
    """按创建时间倒序分页查账本。file_state 由调用方（路由层）用线程池补，避免阻塞事件循环。"""
    page = max(1, int(page))
    page_size = min(200, max(1, int(page_size)))
    conds = _conditions(status=status, task_id=task_id, keyword=keyword)
    with session_scope() as session:
        total = session.exec(select(func.count()).select_from(DownloadRecord).where(*conds)).one()
        rows = session.exec(
            select(DownloadRecord)
            .where(*conds)
            .order_by(col(DownloadRecord.created_at).desc(), col(DownloadRecord.id).desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        items = [r.model_dump() for r in rows]
    return {"items": items, "total": int(total)}


def get_record(record_id: int) -> dict | None:
    with session_scope() as session:
        row = session.get(DownloadRecord, int(record_id))
        return row.model_dump() if row else None
```

- [ ] **Step 5: 路由（注意声明顺序）**

`backend/api/routes_downloads.py`：历史相关路由**必须写在 `/{job_id}` 模式之前**，否则 `POST /api/downloads/history/prune` 会被 `POST /api/downloads/{job_id}/{action}` 抢先匹配、`DELETE /api/downloads/history/{id}` 会把 `history` 当成 job_id。文件顶部补 `import asyncio`。

在 `@router.get("/downloads")` 之后、`/downloads/{job_id}/stop` 之前插入：

```python
@router.get("/downloads/history")
async def history_list(page: int = 1, page_size: int = 50, status: str = "", task_id: int | None = None,
                       keyword: str = "") -> dict:
    from ..api.deps import get_setting
    from ..services import download_history
    from ..services.download_service import DownloadSettings

    cfg = DownloadSettings.from_dict(get_setting("download"))
    await download_history.reconcile(cfg)
    data = await asyncio.to_thread(
        download_history.list_records, page=page, page_size=page_size, status=status, task_id=task_id, keyword=keyword
    )
    states = await asyncio.to_thread(lambda: [download_history.file_state(i["dest_path"]) for i in data["items"]])
    for item, state in zip(data["items"], states):
        item["file_state"] = state
    return data
```

`reconcile` 在 Task 4 实现，本 Task 先加占位实现避免路由 500——**在 `download_history.py` 里写：**

```python
async def reconcile(cfg) -> None:
    """Task 4 实现非终态对账；此处先保持无副作用。"""
    return None
```

`GET /api/downloads` 保持函数体不变（`registry.snapshot()` 语义已由 Task 3 Step 3 收窄）。

- [ ] **Step 6: 端点测试**

`backend/tests/test_download_api.py` 追加：

```python
def test_downloads_endpoint_excludes_terminal_jobs():
    from backend.core.download_registry import DownloadRegistry as _R  # noqa: F401

    jid = registry.create(task_id=9, taskname="x", filename="term.mp4", dest_path="/d/term.mp4", total=10)
    registry.update(jid, done=10, status="done")
    try:
        with TestClient(app) as client:
            ids = {j["id"] for j in client.get("/api/downloads").json()["jobs"]}
    finally:
        registry.remove(jid)
    assert jid not in ids  # 终态不再混进进行中列表


def test_history_endpoint_shape(tmp_path):
    from backend.services import download_history as hist

    rid = hist.start(
        source="builtin", ref_id="api-shape", task_id=41, taskname="T", filename="在.mp4",
        dest_path=str(tmp_path / "在.mp4"), size_total=10, fid="F", driver_key="fake", account_id=None,
    )
    hist.finish("api-shape", source="builtin", status="done", size_done=10)
    (tmp_path / "在.mp4").write_bytes(b"0123456789")
    hist.start(
        source="builtin", ref_id="api-lost", task_id=41, taskname="T", filename="丢.mp4",
        dest_path=str(tmp_path / "丢.mp4"), size_total=10, fid="G", driver_key="fake", account_id=None,
    )
    hist.finish("api-lost", source="builtin", status="done", size_done=10)

    with TestClient(app) as c:
        data = c.get("/api/downloads/history", params={"task_id": 41, "page_size": 10}).json()
    assert data["total"] == 2
    by_name = {i["filename"]: i for i in data["items"]}
    assert by_name["在.mp4"]["file_state"] == "ok"
    assert by_name["丢.mp4"]["file_state"] == "missing"
    assert by_name["在.mp4"]["id"] == rid


def test_history_route_not_shadowed_by_job_id_routes():
    """历史路由必须先声明，否则 /downloads/history/... 会被 /{job_id} 吃掉。"""
    with TestClient(app) as c:
        assert c.get("/api/downloads/history").status_code == 200
        assert c.delete("/api/downloads/history/999999").status_code == 404
```

`DELETE /api/downloads/history/{id}` 在 Step 7 实现（本任务一并加，形状简单）。

- [ ] **Step 7: 加 `DELETE /api/downloads/history/{record_id}`**

`download_history.py` 加：

```python
def delete_record(record_id: int) -> bool:
    """删账本记录，不动磁盘文件。"""
    with session_scope() as session:
        row = session.get(DownloadRecord, int(record_id))
        if row is None:
            return False
        session.delete(row)
    return True
```

路由（紧跟 `history_list` 之后，仍在 `/{job_id}` 之前）：

```python
@router.delete("/downloads/history/{record_id}")
async def history_delete(record_id: int) -> dict:
    from ..services import download_history

    if not download_history.delete_record(record_id):
        raise HTTPException(404, "记录不存在")
    return {"ok": True}
```

- [ ] **Step 8: 跑测试确认通过**

Run: `python3 -m pytest backend/tests -q`
Expected: 全绿（`test_downloads_endpoint_returns_snapshot` / `test_downloads_endpoint_merges_builtin_and_aria2` 仍应通过——它们只断言进行中的 job 在列表里）。

- [ ] **Step 9: ruff + 提交**

```bash
python3 -m ruff check backend
git add backend/core/download_registry.py backend/services/download_history.py backend/api/routes_downloads.py backend/tests
git commit -m "feat(download): 历史账本查询端点，进行中列表不再混入终态记录"
```

---

## Task 4: 非终态对账 `reconcile()`

**Files:**
- Modify: `backend/services/download_history.py`（替换 Task 3 的占位 `reconcile`，加 `open_records`）
- Test: `backend/tests/test_download_history.py`

**Interfaces:**
- Consumes: `aria2_status(cfg)`（`download_service.py:264`）、`aria2_rpc(cfg, method, *params)`（`download_service.py:254`）、`registry.get()`
- Produces:
  - `download_history.open_records() -> list[dict]`（非终态记录）
  - `download_history.reconcile(cfg) -> None`（async，就地更新记录）
  - `download_history.STALE_HOURS: int = 24`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_download_history.py` 追加。先加四个共用助手：

```python
def _seed_open(ref: str, *, source: str = "aria2", dest: str = "/d/x.mkv", size: int = 100) -> int:
    return hist.start(
        source=source, ref_id=ref, task_id=901, taskname="T", filename="x.mkv", dest_path=dest,
        size_total=size, fid="F", driver_key="fake", account_id=None,
    )


def _age(ref: str, hours: float) -> None:
    with session_scope() as s:
        row = s.exec(select(DownloadRecord).where(DownloadRecord.ref_id == ref)).first()
        row.created_at = datetime.now() - timedelta(hours=hours)
        s.add(row)


def _aria2_cfg() -> dl.DownloadSettings:
    return dl.DownloadSettings(mode="aria2", aria2_host_port="http://127.0.0.1:6800")


def _patch_aria2(monkeypatch, *, active: list[dict], struct):
    """active：aria2_status 返回的进行中 job（用 id 标识 gid）；struct：tellDownloadResult 单条结果。"""
    calls: list[tuple] = []

    async def fake_status(cfg):
        return active

    async def fake_rpc(cfg, method, *params):
        calls.append((method, params))
        if struct is None:
            return {"result": [{"errorMessage": "gid not found"}]}
        return {"result": [{"result": [struct]}]}

    monkeypatch.setattr(dl, "aria2_status", fake_status)
    monkeypatch.setattr(dl, "aria2_rpc", fake_rpc)
    return calls


def _status_of(ref: str) -> DownloadRecord:
    with session_scope() as s:
        return s.exec(select(DownloadRecord).where(DownloadRecord.ref_id == ref)).first()
```

对账用例（`system.multicall` 的返回按 asked gid 的顺序一一对应，用例里每次只问一个 gid）：

```python
async def test_reconcile_aria2_complete_marks_done(tmp_path, monkeypatch):
    _seed_open("g-ok", dest=str(tmp_path / "x.mkv"))
    _patch_aria2(monkeypatch, active=[], struct={"status": "complete", "completedLength": "100", "totalLength": "100"})
    await hist.reconcile(_aria2_cfg())
    r = _status_of("g-ok")
    assert r.status == "done" and r.size_done == 100 and r.finished_at is not None


async def test_reconcile_aria2_error_marks_failed_with_message(tmp_path, monkeypatch):
    _seed_open("g-err", dest=str(tmp_path / "y.mkv"))
    _patch_aria2(monkeypatch, active=[], struct={"status": "error", "error_message": "直链过期", "completedLength": "10"})
    await hist.reconcile(_aria2_cfg())
    r = _status_of("g-err")
    assert r.status == "failed" and r.error == "直链过期"


async def test_reconcile_gid_still_running_keeps_queued(tmp_path, monkeypatch):
    _seed_open("g-run", dest=str(tmp_path / "z.mkv"))
    calls = _patch_aria2(monkeypatch, active=[{"id": "g-run", "status": "downloading"}], struct=None)
    await hist.reconcile(_aria2_cfg())
    assert _status_of("g-run").status == "queued"
    assert calls == []  # 仍在跑的不该去问 tellDownloadResult


async def test_reconcile_falls_back_to_file_stat(tmp_path, monkeypatch):
    dest = tmp_path / "already.mkv"
    dest.write_bytes(b"0" * 100)
    _seed_open("g-file", dest=str(dest))
    _patch_aria2(monkeypatch, active=[], struct=None)  # gid 已被 aria2 丢弃
    await hist.reconcile(_aria2_cfg())
    r = _status_of("g-file")
    assert r.status == "done" and r.size_done == 100


async def test_reconcile_partial_file_stays_queued_until_stale(tmp_path, monkeypatch):
    dest = tmp_path / "half.mkv"
    dest.write_bytes(b"0" * 50)  # 大小不符，不能算完成
    _seed_open("g-half", dest=str(dest))
    _patch_aria2(monkeypatch, active=[], struct=None)
    await hist.reconcile(_aria2_cfg())
    assert _status_of("g-half").status == "queued"


async def test_reconcile_builtin_orphan_young_keeps_queued(tmp_path, monkeypatch):
    """内置 job 随进程重启消失、文件也不在：不足 24h 先不动。"""
    _seed_open("b-new", source="builtin", dest=str(tmp_path / "none.mkv"))
    _patch_aria2(monkeypatch, active=[], struct=None)
    await hist.reconcile(_aria2_cfg())
    assert _status_of("b-new").status == "queued"


async def test_reconcile_builtin_orphan_stale_marks_failed(tmp_path, monkeypatch):
    _seed_open("b-old", source="builtin", dest=str(tmp_path / "none2.mkv"))
    _age("b-old", 25)
    _patch_aria2(monkeypatch, active=[], struct=None)
    await hist.reconcile(_aria2_cfg())
    r = _status_of("b-old")
    assert r.status == "failed" and "对账超时" in r.error


async def test_reconcile_adopts_builtin_terminal_from_registry(tmp_path, monkeypatch):
    """内存 registry 已有终态但库里还挂着：以 registry 为准补写（finish 漏写时自愈）。"""
    jid = registry.create(task_id=901, taskname="T", filename="f.mkv", dest_path="/d/f.mkv", total=10)
    _seed_open(jid, source="builtin")
    registry.update(jid, done=10, status="stopped", error="已停止")
    _patch_aria2(monkeypatch, active=[], struct=None)
    await hist.reconcile(_aria2_cfg())
    assert _status_of(jid).status == "stopped"
    registry.remove(jid)


async def test_reconcile_skips_aria2_when_mode_is_builtin(tmp_path, monkeypatch):
    """内置模式下不该去敲 aria2 RPC（cfg.mode 非 aria2 时直接走兜底）。"""
    _seed_open("g-off", source="aria2", dest=str(tmp_path / "off.mkv"))
    calls = _patch_aria2(monkeypatch, active=[], struct={"status": "complete"})
    await hist.reconcile(dl.DownloadSettings(mode="builtin"))
    assert calls == []
    assert _status_of("g-off").status == "queued"  # 既没问 RPC 也没文件，先挂着
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest backend/tests/test_download_history.py -q -k reconcile`
Expected: FAIL —占位 `reconcile` 不改状态，断言 `status == "done"` 失败。

- [ ] **Step 3: 实现 `open_records` + `reconcile`**

`backend/services/download_history.py`：把 Task 3 的占位 `reconcile` 换成下面两函数，顶部加 `import asyncio` 与 `from datetime import datetime, timedelta`。

```python
STALE_HOURS = 24
ACTIVE_STATES = ("queued", "downloading")


def open_records() -> list[dict]:
    with session_scope() as session:
        rows = session.exec(select(DownloadRecord).where(DownloadRecord.status.in_(ACTIVE_STATES))).all()
        return [r.model_dump() for r in rows]


async def reconcile(cfg) -> None:
    """收口非终态记录：aria2 问 RPC → 文件 stat 兜底 → 超 24h 判失败。

    cfg 为 DownloadSettings；调用方（路由层）负责读配置，本函数不碰 setting。
    延迟导入 download_service 是为了避开与写入点的循环导入。
    """
    from ..core.download_registry import registry
    from .download_service import aria2_rpc, aria2_status

    rows = open_records()
    if not rows:
        return

    running: set[str] = set()
    results: dict[str, dict] = {}
    if cfg.mode == "aria2" and cfg.aria2_host_port:
        try:
            running = {j["id"] for j in await aria2_status(cfg)}
        except Exception:  # noqa: BLE001 aria2 不可达时静默降级到文件兜底
            running = set()
        asked = [r["ref_id"] for r in rows if r["source"] == "aria2" and r["ref_id"] not in running]
        if asked:
            try:
                resp = await aria2_rpc(
                    cfg, "system.multicall",
                    [{"methodName": "aria2.tellDownloadResult", "params": [g]} for g in asked],
                )
                for gid, entry in zip(asked, resp.get("result") or []):
                    if "errorMessage" in entry:
                        continue  # gid 结果已被 aria2 丢弃，走文件兜底
                    struct = (entry.get("result") or [None])[0]
                    if struct:
                        results[gid] = struct
            except Exception:  # noqa: BLE001
                results = {}

    now = datetime.now()
    for r in rows:
        ref, source = r["ref_id"], r["source"]
        skip = False
        if source == "builtin":
            job = registry.get(ref)
            if job is None:
                pass  # 进程重启后内存清空，落到文件兜底
            elif job.status in TERMINAL:
                finish(ref, source=source, status=job.status, size_done=job.done, size_total=job.total,
                       error=job.error)
                continue
            else:
                skip = True  # 本进程还在下，进行中 tab 负责展示
        elif cfg.mode == "aria2" and cfg.aria2_host_port:
            if ref in running:
                skip = True  # aria2 仍在跑/排队，不动
            else:
                struct = results.get(ref)
                if struct is not None:
                    state = str(struct.get("status") or "")
                    if state == "complete":
                        finish(ref, source=source, status="done", size_done=int(struct.get("completedLength") or 0),
                               size_total=int(struct.get("totalLength") or r["size_total"] or 0))
                        continue
                    if state in ("error", "removed"):
                        finish(ref, source=source, status="failed",
                               size_done=int(struct.get("completedLength") or 0),
                               error=str(struct.get("error_message") or "aria2 未成功"))
                        continue

        if skip:
            continue
        # 兜底：文件到位 = 完成
        state = await asyncio.to_thread(file_state, r["dest_path"])
        matches = await asyncio.to_thread(_size_matches, r["dest_path"], int(r["size_total"] or 0))
        if state == "ok" and matches:
            finish(ref, source=source, status="done", size_done=r["size_total"], size_total=r["size_total"])
            continue
        if now - r["created_at"] >= timedelta(hours=STALE_HOURS):
            finish(ref, source=source, status="failed",
                   error=f"对账超时：下载器无响应或结果已丢弃（超过 {STALE_HOURS} 小时未确认）")


def _size_matches(path: str, size_total: int) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    return st.st_size == size_total if size_total else st.st_size > 0
```

`reconcile` 里 `cfg.mode != "aria2"` 时**完全不碰 aria2 RPC**（Task 4 有专门用例断言调用次数为 0）。
`file_state` 与 `_size_matches` 是同步阻塞函数，一律经 `asyncio.to_thread` 调用。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest backend/tests/test_download_history.py -q`
Expected: 全绿。注意 `test_reconcile_gid_still_running_keeps_queued` 里 `fake_status` 返回的 dict 必须含 `"id"` 键（与 `aria2_status()` 真实结构一致）。

- [ ] **Step 5: 全量测试 + ruff + 提交**

```bash
python3 -m pytest backend/tests -q
python3 -m ruff check backend
git add backend/services/download_history.py backend/tests/test_download_history.py
git commit -m "feat(download): 非终态记录对账，aria2 终态回填 + 重启遗留按文件与 24h 收口"
```

---

## Task 5: 历史 tab 界面

**Files:**
- Modify: `frontend/src/api/types.ts:237-251`（加 `DownloadRecord`）
- Modify: `frontend/src/api/client.ts:146-152`（加 3 个接口）
- Modify: `frontend/src/views/DownloadsView.vue`（整文件重写为双 tab）
- Test: `cd frontend && npm run typecheck`（项目无前端单测框架，以类型检查 + 真机验证代替）

**Interfaces:**
- Consumes: `GET /api/downloads/history`、`DELETE /api/downloads/history/{id}`（Task 3）
- Produces: `DownloadRecord` 前端类型、`api.listDownloadHistory(params)`、`api.deleteDownloadHistory(id)`

- [ ] **Step 1: 加类型**

`frontend/src/api/types.ts` 在 `DownloadJob` 之后追加：

```ts
export type FileState = "ok" | "missing" | "unknown";

export interface DownloadRecord {
  id: number;
  source: "builtin" | "aria2";
  ref_id: string;
  task_id: number | null;
  taskname: string;
  filename: string;
  dest_path: string;
  size_total: number;
  size_done: number;
  fid: string;
  driver_key: string;
  account_id: number | null;
  status: "queued" | "downloading" | "done" | "failed" | "skipped" | "stopped";
  error: string;
  created_at: string;
  finished_at: string | null;
  file_state?: FileState;
}

export interface DownloadHistoryQuery {
  page?: number;
  page_size?: number;
  status?: string;
  task_id?: number | null;
  keyword?: string;
}
```

- [ ] **Step 2: 加 client 方法**

`frontend/src/api/client.ts` 在 `deleteDownload` 之后追加（文件顶部 `import type` 补 `DownloadHistoryQuery, DownloadRecord`）：

```ts
  listDownloadHistory: (q: DownloadHistoryQuery) => {
    const p = new URLSearchParams();
    p.set("page", String(q.page ?? 1));
    p.set("page_size", String(q.page_size ?? 50));
    if (q.status) p.set("status", q.status);
    if (q.task_id) p.set("task_id", String(q.task_id));
    if (q.keyword) p.set("keyword", q.keyword);
    return request<{ items: DownloadRecord[]; total: number }>(`/api/downloads/history?${p}`);
  },
  deleteDownloadHistory: (id: number) =>
    request<{ ok: boolean }>(`/api/downloads/history/${id}`, { method: "DELETE" }),
```

- [ ] **Step 3: 重写 DownloadsView.vue**

完整替换 `frontend/src/views/DownloadsView.vue`。保留现有 `pct/fmtSize/progressStatus/statusText` 与 `act/del`（进行中 tab 用），新增历史相关状态与模板：

```vue
<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { api } from "../api/client";
import { useTasksStore } from "../stores/tasks";
import type { DownloadJob, DownloadRecord } from "../api/types";

const tab = ref<"active" | "history">("active");
const tasks = useTasksStore();

/* ---------- 进行中 ---------- */
const jobs = ref<DownloadJob[]>([]);
const error = ref("");
let timer: number | undefined;

async function refresh() {
  if (document.hidden) return;
  try {
    jobs.value = (await api.listDownloads()).jobs;
    error.value = "";
  } catch (e) {
    error.value = (e as Error).message;
  }
}

async function act(row: DownloadJob, action: "stop" | "pause" | "resume") {
  try {
    await api.downloadAction(row.id, action, row.source);
    refresh();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

async function del(row: DownloadJob) {
  try {
    await api.deleteDownload(row.id, row.source);
    refresh();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

/* ---------- 历史 ---------- */
const rows = ref<DownloadRecord[]>([]);
const total = ref(0);
const hLoading = ref(false);
const query = reactive({ page: 1, page_size: 50, status: [] as string[], task_id: null as number | null, keyword: "" });
const statusOptions = [
  { label: "排队", value: "queued" },
  { label: "下载中", value: "downloading" },
  { label: "完成", value: "done" },
  { label: "失败", value: "failed" },
  { label: "跳过", value: "skipped" },
  { label: "已停止", value: "stopped" },
];
let hTimer: number | undefined;

const hasOpen = computed(() => rows.value.some((r) => r.status === "queued" || r.status === "downloading"));

function stateText(s?: string): string {
  return s === "ok" ? "在" : s === "missing" ? "已丢失" : "未校验";
}

async function loadHistory() {
  hLoading.value = true;
  try {
    const r = await api.listDownloadHistory({
      page: query.page,
      page_size: query.page_size,
      status: query.status.join(","),
      task_id: query.task_id,
      keyword: query.keyword.trim(),
    });
    rows.value = r.items;
    total.value = r.total;
  } catch (e) {
    ElMessage.error((e as Error).message);
  } finally {
    hLoading.value = false;
  }
}

async function delRecord(row: DownloadRecord) {
  try {
    await api.deleteDownloadHistory(row.id);
    ElMessage.success("已删除记录（磁盘文件保留）");
    loadHistory();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}

watch(tab, (v) => {
  if (v === "history") loadHistory();
});
watch(
  () => [query.status.join(","), query.task_id, query.page, query.page_size],
  () => tab.value === "history" && loadHistory(),
);
let kwTimer: number | undefined;
watch(
  () => query.keyword,
  () => {
    window.clearTimeout(kwTimer);
    kwTimer = window.setTimeout(() => {
      query.page = 1;
      if (tab.value === "history") loadHistory();
    }, 400);
  },
);
watch(hasOpen, (open) => {
  window.clearInterval(hTimer);
  if (open && tab.value === "history") hTimer = window.setInterval(loadHistory, 5000);
});

function fmtSize(n: number): string {
  if (n <= 0) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${u[i]}`;
}

function pct(j: DownloadJob): number {
  if (!j.total) return j.status === "done" ? 100 : 0;
  return Math.min(100, Math.round((j.done / j.total) * 100));
}

function progressStatus(s: string): "" | "success" | "exception" | "warning" {
  if (s === "done" || s === "skipped") return "success";
  if (s === "failed") return "exception";
  return "";
}

function statusText(s: string): string {
  return (
    { queued: "排队", downloading: "下载中", paused: "已暂停", stopped: "已停止", done: "完成", failed: "失败", skipped: "跳过" } as Record<string, string>
  )[s] || s;
}

function fmtTime(iso: string | null): string {
  return iso ? iso.replace("T", " ").slice(0, 19) : "-";
}

onMounted(() => {
  refresh();
  tasks.fetchTasks();
  timer = window.setInterval(refresh, 1500);
  document.addEventListener("visibilitychange", onVisible);
});
onBeforeUnmount(() => {
  window.clearInterval(timer);
  window.clearInterval(hTimer);
  document.removeEventListener("visibilitychange", onVisible);
});

function onVisible() {
  if (!document.hidden) refresh();
}
</script>

<template>
  <div>
    <div class="sticky-bar">
      <span class="sticky-bar__title">下载任务</span>
      <span v-if="error" class="err">{{ error }}</span>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane name="active" :label="`进行中 (${jobs.length})`">
        <el-table :data="jobs" empty-text="暂无进行中的下载" row-key="id">
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
          <el-table-column label="操作" width="220">
            <template #default="{ row }">
              <template v-if="row.status === 'downloading' || row.status === 'queued' || row.status === 'paused'">
                <el-button v-if="row.source === 'aria2' && row.status !== 'paused'" size="small" text @click="act(row, 'pause')">暂停</el-button>
                <el-button v-if="row.source === 'aria2' && row.status === 'paused'" size="small" text @click="act(row, 'resume')">继续</el-button>
                <el-button size="small" text type="warning" @click="act(row, 'stop')">停止</el-button>
              </template>
              <el-button size="small" text type="danger" @click="del(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane name="history" label="历史">
        <div class="filters">
          <el-select v-model="query.status" multiple collapse-tags placeholder="全部状态" clearable style="width: 200px">
            <el-option v-for="o in statusOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
          <el-select v-model="query.task_id" placeholder="全部任务" clearable filterable style="width: 200px">
            <el-option v-for="t in tasks.sorted" :key="t.id" :label="t.taskname" :value="t.id" />
          </el-select>
          <el-input v-model="query.keyword" placeholder="文件名 / 路径" clearable style="width: 240px" />
        </div>

        <el-table v-loading="hLoading" :data="rows" empty-text="暂无下载记录" row-key="id">
          <el-table-column prop="taskname" label="任务" min-width="120" show-overflow-tooltip />
          <el-table-column prop="filename" label="文件" min-width="200" show-overflow-tooltip />
          <el-table-column label="体积" width="130">
            <template #default="{ row }">{{ fmtSize(row.size_done) }} / {{ row.size_total ? fmtSize(row.size_total) : "未知" }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="row.status === 'failed' ? 'danger' : row.status === 'done' ? 'success' : 'info'" size="small">
                {{ statusText(row.status) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="文件" width="90">
            <template #default="{ row }">
              <span :class="row.file_state === 'missing' ? 'lost' : ''">{{ stateText(row.file_state) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="完成时间" width="160">
            <template #default="{ row }">{{ fmtTime(row.finished_at) }}</template>
          </el-table-column>
          <el-table-column prop="dest_path" label="目标路径" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="sub">{{ row.dest_path }}</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="{ row }">
              <el-button size="small" text type="danger" @click="delRecord(row)">删记录</el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-pagination
          v-model:current-page="query.page"
          v-model:page-size="query.page_size"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          style="margin-top: 12px; justify-content: flex-end"
        />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.err { color: var(--danger); font-size: 13px; margin-left: 12px; }
.sub { font-size: 12px; color: var(--text-muted); }
.filters { display: flex; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.lost { color: var(--danger); font-size: 13px; }
</style>
```

本 Task 的历史表只有「删记录」一个操作；「重下」按钮和 `retry()` 在 Task 6 接入（那里一并加 `el-table-column` 宽度从 110 调到 170）。

- [ ] **Step 4: 类型检查**

Run: `cd frontend && npm run typecheck`
Expected: 无错误。

- [ ] **Step 5: 真机验证**

后端起 `python3 -m uvicorn backend.main:app --port 8000`，前端 `cd frontend && npm run dev`，浏览器打开 `/downloads`：
切换两个 tab、验证历史筛选（状态/任务/关键词）、翻页、删记录、文件列显示"在/已丢失"。

- [ ] **Step 6: 提交**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts frontend/src/views/DownloadsView.vue
git commit -m "feat(ui): 下载页拆成进行中/历史双 tab，历史支持筛选分页与到位校验列"
```

---

## Task 6: `download_items()` 抽取与单文件重下

**Files:**
- Modify: `backend/services/download_service.py`（抽函数 + `retry_record` + `_account_for`）
- Modify: `backend/api/routes_downloads.py`（retry 端点，写在 `/{job_id}` 之前）
- Modify: `frontend/src/api/client.ts`、`frontend/src/views/DownloadsView.vue`
- Test: `backend/tests/test_download_history.py`（retry 服务层）、`backend/tests/test_download_api.py`（端点）

**Interfaces:**
- Consumes: `DownloadItem`、`history.get_record`、`_primary_account` 选账号语义（`routes_files.py:21`）
- Produces:
  - `download_service.download_items(driver, items, cfg, *, log, task_id=None, taskname="", account_id=None, driver_key="") -> list[str]`
  - `download_service.retry_record(rec: dict, cfg: DownloadSettings, *, log) -> None`
  - `POST /api/downloads/history/{record_id}/retry` → `{"ok": true, "message": "重下已开始"}`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_download_history.py` 追加：

```python
class _Acc:
    id, cookie, sort_order, driver_key = 1, "ck", 0, "fake"


async def test_retry_record_keeps_original_dest_path(tmp_path, monkeypatch):
    """重下必须打回原目标路径，不能被 resolve_local 按网盘目录重算。"""
    dest = str(tmp_path / "外部目录" / "已存在.mkv")
    rid = hist.start(
        source="builtin", ref_id="retry-src", task_id=921, taskname="T", filename="已存在.mkv",
        dest_path=dest, size_total=10, fid="FID1", driver_key="fake", account_id=None,
    )
    hist.finish("retry-src", source="builtin", status="failed", error="HTTP 500")
    seen = []

    async def fake_items(driver, items, cfg, *, log, task_id=None, taskname="", account_id=None, driver_key=""):
        seen.append((items[0].fid, str(items[0].local_path), taskname, account_id))
        return [f"✅ {items[0].name}"]

    monkeypatch.setattr(dl, "download_items", fake_items)
    monkeypatch.setattr(dl, "_account_for", lambda rec: _Acc())
    await dl.retry_record(hist.get_record(rid), dl.DownloadSettings(dir=str(tmp_path)), log=lambda *a, **k: None)
    assert seen == [("FID1", dest, "T", 1)]


async def test_retry_without_account_logs_and_returns(tmp_path, monkeypatch):
    rid = hist.start(
        source="builtin", ref_id="retry-noacc", task_id=923, taskname="t", filename="a", dest_path="/d/a",
        size_total=1, fid="F", driver_key="fake", account_id=None,
    )
    calls = []

    async def fake_items(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(dl, "download_items", fake_items)
    monkeypatch.setattr(dl, "_account_for", lambda rec: None)
    msgs = []
    await dl.retry_record(hist.get_record(rid), dl.DownloadSettings(), log=lambda lvl, m: msgs.append(m))
    assert calls == [] and any("账号" in m for m in msgs)


async def test_retry_with_unsupported_driver_logs_and_returns(tmp_path, monkeypatch):
    from backend.drivers import get_driver_class

    rid = hist.start(
        source="builtin", ref_id="retry-nodrv", task_id=924, taskname="t", filename="a", dest_path="/d/a",
        size_total=1, fid="F", driver_key="no_such_drive", account_id=None,
    )
    calls = []

    async def fake_items(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(dl, "download_items", fake_items)
    assert get_driver_class("no_such_drive") is None
    msgs = []
    await dl.retry_record(hist.get_record(rid), dl.DownloadSettings(), log=lambda lvl, m: msgs.append(m))
    assert calls == [] and any("驱动" in m for m in msgs)
```

`backend/tests/test_download_api.py` 追加（文件顶部补 `import time`）：

```python
def test_retry_endpoint_returns_at_once(monkeypatch):
    """端点必须立刻返回：内置下载器一个 4K 文件可能跑几小时，绝不同步等待。"""
    from backend.services import download_history as hist

    async def slow_retry(rec, cfg, *, log):
        await asyncio.sleep(30)

    monkeypatch.setattr(dl, "retry_record", slow_retry)
    rid = hist.start(
        source="builtin", ref_id="api-retry", task_id=925, taskname="t", filename="f", dest_path="/d/f",
        size_total=1, fid="F", driver_key="fake", account_id=None,
    )
    t0 = time.monotonic()
    with TestClient(app) as c:
        r = c.post(f"/api/downloads/history/{rid}/retry")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert time.monotonic() - t0 < 2


def test_retry_unknown_record_404():
    with TestClient(app) as c:
        assert c.post("/api/downloads/history/987654/retry").status_code == 404
```

文件顶部补 `import asyncio`。`hist.start` 造的记录 `driver_key="fake"`，`get_driver_class("fake")` 返回 None，因此端点**只做记录存在性校验 + 起后台任务**，驱动解析留在 `retry_record` 内——否则第一个用例会 500。

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest backend/tests/test_download_history.py backend/tests/test_download_api.py -q -k retry`
Expected: FAIL —`download_items` / `retry_record` / 端点不存在（404 断言会以"路由不存在"或属性错误形式失败）。

- [ ] **Step 3: 抽 `download_items()`**

`backend/services/download_service.py`，把 `download_task_files` 的分派段搬进新函数，`download_task_files` 只剩"清单 + 执行"：

```python
async def download_items(
    driver: CloudDrive, items: list[DownloadItem], cfg: DownloadSettings, *, log: LogFn,
    task_id: int | None = None, taskname: str = "", account_id: int | None = None, driver_key: str = "",
) -> list[str]:
    """执行已解析的下载清单：分派内置/aria2、写账本、成功后刷新 Emby。"""
    if cfg.mode == "aria2" and not await aria2_reachable(cfg):
        log("warn", "⚠️ aria2 不可达，自动改用内置下载器（保证下载不中断）")
        cfg = _as_builtin(cfg)
    if cfg.mode == "aria2":
        lines = await _aria2_submit(
            driver, items, cfg, log, task_id=task_id, taskname=taskname, account_id=account_id, driver_key=driver_key
        )
    else:
        lines = await _builtin_download(
            driver, items, cfg, log, task_id=task_id, taskname=taskname, account_id=account_id, driver_key=driver_key
        )
    if any(line.startswith("✅") for line in lines):
        await _emby_refresh(cfg, log)
    return lines


async def download_task_files(
    driver, saved, cfg, *, download_subdir=False, savepath_override="", log=None,
    task_id=None, taskname="", account_id=None,
) -> list[str]:
    """转存成功后的文件落本地：解析清单 → 执行下载。返回摘要行（供通知聚合）。"""
    log = log or (lambda level, msg: None)
    if not saved:
        return []
    items = await collect_files(driver, saved, cfg, savepath_override, download_subdir)
    if not items:
        return []
    return await download_items(
        driver, items, cfg, log=log, task_id=task_id, taskname=taskname,
        account_id=account_id, driver_key=driver.key,
    )
```

- [ ] **Step 4: `_account_for` + `retry_record`**

同文件末尾追加：

```python
def _account_for(rec: dict):
    """重下用账号：优先记录里的 account_id，失效则按 driver_key 选可用主账号（语义同 routes_files._primary_account）。"""
    from sqlmodel import select

    from ..database import session_scope
    from ..models import Account

    with session_scope() as session:
        acc = session.get(Account, int(rec["account_id"])) if rec.get("account_id") else None
        if acc is not None and acc.enabled:
            return acc
        accs = session.exec(
            select(Account)
            .where(Account.enabled, Account.driver_key == rec["driver_key"])
            .order_by(Account.sort_order, Account.id)
        ).all()
        return next((a for a in accs if a.can_save), accs[0] if accs else None)


async def retry_record(rec: dict, cfg: DownloadSettings, *, log: LogFn) -> None:
    """按账本记录重下单个文件：目标路径原样保留，直链重新获取。"""
    from ..drivers import get_driver_class

    cls = get_driver_class(rec.get("driver_key") or "")
    if cls is None or not cls.supported:
        log("error", f"《{rec.get('taskname') or ''}》重下失败：{rec.get('driver_key')} 驱动不可用")
        return
    acc = _account_for(rec)
    if acc is None:
        log("error", f"《{rec.get('taskname') or ''}》重下失败：没有可用的 {rec.get('driver_key')} 账号")
        return
    driver = cls(cookie=acc.cookie, proxy=PROXY, index=acc.sort_order)
    if not driver.has("download"):
        log("error", f"《{rec.get('taskname') or ''}》重下失败：{driver.name} 不支持下载")
        return
    item = DownloadItem(
        fid=rec["fid"], name=rec["filename"], size=int(rec["size_total"] or 0), local_path=Path(rec["dest_path"])
    )
    lines = await download_items(
        driver, [item], cfg, log=log, task_id=rec.get("task_id"), taskname=rec.get("taskname") or "",
        account_id=acc.id, driver_key=rec["driver_key"],
    )
    log("info" if any(l.startswith("✅") for l in lines) else "warn", f"🔁 重下 {rec['filename']}：{lines or '无结果'}")
```

`download_service.py` 顶部补 `from ..config import PROXY`（`retry_record` 构造驱动要用）；`get_driver_class` 保持函数内 `from ..drivers import get_driver_class`，与仓库既有的延迟导入风格一致。

- [ ] **Step 5: retry 端点**

`backend/api/routes_downloads.py`，插在 `history_delete` 之后、`/{job_id}/stop` 之前：

```python
@router.post("/downloads/history/{record_id}/retry")
async def history_retry(record_id: int) -> dict:
    """起后台任务重下：内置下载器一个 4K 文件可能跑几小时，绝不同步等待。"""
    import asyncio

    from ..api.deps import get_setting
    from ..core.logstream import hub
    from ..services import download_history
    from ..services.download_service import DownloadSettings, retry_record

    rec = download_history.get_record(record_id)
    if rec is None:
        raise HTTPException(404, "记录不存在")
    cfg = DownloadSettings.from_dict(get_setting("download"))
    log = hub.make_logger("retry", task_id=rec.get("task_id"))
    asyncio.create_task(retry_record(rec, cfg, log=log))
    return {"ok": True, "message": "重下已开始，稍后刷新查看"}
```

- [ ] **Step 6: 前端接重下**

`frontend/src/api/client.ts` 在 `deleteDownloadHistory` 后加：

```ts
  retryDownloadHistory: (id: number) =>
    request<{ ok: boolean; message: string }>(`/api/downloads/history/${id}/retry`, { method: "POST" }),
```

`DownloadsView.vue` 历史表操作列改成两个按钮（列宽 110 → 170）：

```vue
          <el-table-column label="操作" width="170">
            <template #default="{ row }">
              <el-button size="small" text type="primary" @click="retry(row)">重下</el-button>
              <el-button size="small" text type="danger" @click="delRecord(row)">删记录</el-button>
            </template>
          </el-table-column>
```

script 里 `delRecord` 之后加：

```ts
async function retry(row: DownloadRecord) {
  try {
    const r = await api.retryDownloadHistory(row.id);
    ElMessage.success(r.message);
    await loadHistory();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}
```

- [ ] **Step 7: 跑测试确认通过**

Run: `python3 -m pytest backend/tests -q && (cd frontend && npm run typecheck)`
Expected: 后端全绿（`test_download.py` 既有用例应仍通过——`download_task_files` 外部行为未变）；前端类型检查无错误。

- [ ] **Step 8: ruff + 提交**

```bash
python3 -m ruff check backend
git add backend/services/download_service.py backend/api/routes_downloads.py backend/tests frontend/src
git commit -m "feat(download): 抽出 download_items 并加单文件重下，失败记录能拉回原路径重下"
```

---

## Task 7: 保留策略与清理

**Files:**
- Modify: `backend/api/deps.py:20-26`（默认值）
- Modify: `backend/services/download_service.py:30-54`（`DownloadSettings` 加字段）
- Modify: `backend/services/download_history.py`（`prune`）
- Modify: `backend/api/routes_downloads.py`（prune 端点）
- Modify: `backend/core/scheduler.py`（`add_daily`）
- Modify: `backend/main.py`（`_prune_job` + 启动一次 + 每日）
- Modify: `frontend/src/api/types.ts`、`frontend/src/components/settings/SettingsDownload.vue`、`frontend/src/api/client.ts`
- Test: `backend/tests/test_download_history.py`、`backend/tests/test_download_api.py`

**Interfaces:**
- Consumes: `DownloadSettings`、`session_scope`
- Produces:
  - `download_history.prune(mode: str, retention: str = "days_90") -> int`
  - `TaskScheduler.add_daily(job_id: str, func, hour: int = 4) -> None`
  - `POST /api/downloads/history/prune` → `{"ok": true, "removed": int}`
  - 设置项 `download.history_retention`：`days_30` / `days_90` / `days_180` / `forever`，默认 `days_90`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_download_history.py` 追加：

```python
def _seed_finished(ref: str, *, status: str = "done", age_days: int = 0) -> int:
    rid = hist.start(
        source="builtin", ref_id=ref, task_id=911, taskname="t", filename=ref, dest_path=f"/d/{ref}",
        size_total=1, fid="F", driver_key="fake", account_id=None,
    )
    hist.finish(ref, source="builtin", status=status, size_done=1)
    if age_days:
        with session_scope() as s:
            row = s.get(DownloadRecord, rid)
            row.finished_at = datetime.now() - timedelta(days=age_days)
            s.add(row)
    return rid


def _has(ref: str) -> bool:
    return any(r.ref_id == ref for r in _rows())


def test_prune_auto_uses_retention_days():
    _seed_finished("p-old", age_days=120)
    _seed_finished("p-new", age_days=1)
    hist.start(  # 非终态记录不受自动清理影响
        source="builtin", ref_id="p-open", task_id=911, taskname="t", filename="p-open", dest_path="/d/p-open",
        size_total=1, fid="F", driver_key="fake", account_id=None,
    )
    assert hist.prune("auto", "days_90") >= 1
    assert not _has("p-old") and _has("p-new") and _has("p-open")


def test_prune_forever_removes_nothing():
    _seed_finished("pf-old", age_days=400)
    assert hist.prune("auto", "forever") == 0
    assert _has("pf-old")


def test_prune_failed_only_removes_failed():
    _seed_finished("x-done", status="done")
    _seed_finished("x-fail", status="failed")
    assert hist.prune("failed") >= 1
    assert _has("x-done") and not _has("x-fail")


def test_prune_all_removes_records():
    _seed_finished("y-done")
    assert hist.prune("all") >= 1
    assert not _has("y-done")
```

`backend/tests/test_download_api.py` 追加：

```python
def test_prune_endpoint_passes_retention_from_setting(monkeypatch):
    from backend.api import deps
    from backend.services import download_history as hist

    seen = {}

    def spy(mode, retention="days_90"):
        seen["args"] = (mode, retention)
        return 3

    monkeypatch.setattr(hist, "prune", spy)
    monkeypatch.setattr(deps, "get_setting", lambda k: {"history_retention": "days_30"})
    with TestClient(app) as c:
        r = c.post("/api/downloads/history/prune", json={"mode": "auto"})
    assert r.json() == {"ok": True, "removed": 3}
    assert seen["args"] == ("auto", "days_30")


def test_prune_endpoint_unknown_mode_defaults_to_auto(monkeypatch):
    from backend.api import deps
    from backend.services import download_history as hist

    seen = {}
    monkeypatch.setattr(hist, "prune", lambda mode, retention="days_90": seen.update({"mode": mode}) or 0)
    monkeypatch.setattr(deps, "get_setting", lambda k: {})
    with TestClient(app) as c:
        assert c.post("/api/downloads/history/prune", json={"mode": "nonsense"}).json()["ok"] is True
    assert seen["mode"] == "nonsense"  # 未识别的 mode 落到 auto 分支，不删任何数据
```

`backend/tests/test_scheduler_log.py` 或新用例里补 `add_daily`：

```python
def test_add_daily_registers_replacing_job():
    from backend.core.scheduler import TaskScheduler

    s = TaskScheduler()
    s.start()
    try:
        s.add_daily("xiao_pan_prune", lambda: None)
        s.add_daily("xiao_pan_prune", lambda: None)
        assert len([j for j in s.scheduler.get_jobs() if j.id == "xiao_pan_prune"]) == 1
    finally:
        s.shutdown()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest backend/tests/test_download_history.py backend/tests/test_download_api.py -q -k prune`
Expected: FAIL —`prune` / `add_daily` 不存在。

- [ ] **Step 3: `prune()`**

`backend/services/download_history.py` 追加：

```python
def _retention_days(retention: str) -> int | None:
    """days_90 → 90；forever / 非法值 → None（不清）。"""
    if not str(retention).startswith("days_"):
        return None
    try:
        return max(1, int(str(retention).split("_", 1)[1]))
    except ValueError:
        return None


def prune(mode: str, retention: str = "days_90") -> int:
    """清理账本。auto=按保留策略删过期终态记录；failed=只删失败；all=清空。返回删除条数。"""
    with session_scope() as session:
        if mode == "all":
            stmt = select(DownloadRecord)
        elif mode == "failed":
            stmt = select(DownloadRecord).where(DownloadRecord.status == "failed")
        else:
            days = _retention_days(retention)
            if days is None:
                return 0
            cutoff = datetime.now() - timedelta(days=days)
            stmt = select(DownloadRecord).where(
                DownloadRecord.status.in_(TERMINAL), DownloadRecord.finished_at < cutoff
            )
        rows = session.exec(stmt).all()
        for row in rows:
            session.delete(row)
        return len(rows)
```

- [ ] **Step 4: 设置项贯通**

`backend/api/deps.py` 的 `DEFAULT_SETTINGS["download"]` 加一行 `"history_retention": "days_90",`。
`backend/services/download_service.py` 的 `DownloadSettings` 加字段与解析：

```python
    history_retention: str = "days_90"  # days_30 | days_90 | days_180 | forever
```

```python
            history_retention=str(raw.get("history_retention") or "days_90"),
```

- [ ] **Step 5: prune 端点**

`backend/api/routes_downloads.py`，加 `from pydantic import BaseModel` 与（仍排在 `/{job_id}` 之前）：

```python
class PrunePayload(BaseModel):
    mode: str = "auto"  # auto | failed | all


@router.post("/downloads/history/prune")
async def history_prune(payload: PrunePayload) -> dict:
    from ..api.deps import get_setting
    from ..services import download_history

    retention = str((get_setting("download") or {}).get("history_retention") or "days_90")
    removed = download_history.prune(payload.mode, retention)
    return {"ok": True, "removed": removed}
```

- [ ] **Step 6: 每日清理 job**

`backend/core/scheduler.py` 在 `reschedule` 之后加：

```python
    def add_daily(self, job_id: str, func, hour: int = 4) -> None:
        """注册一个每天整点执行的维护任务（重复注册覆盖）。"""
        self.scheduler.add_job(
            func, trigger=CronTrigger(hour=hour, minute=0), id=job_id, replace_existing=True,
            max_instances=1, coalesce=True,
        )
```

`backend/main.py` 加：

```python
async def _prune_job() -> None:
    from .api.deps import get_setting
    from .services import download_history

    log = hub.make_logger("prune")
    try:
        retention = str((get_setting("download") or {}).get("history_retention") or "days_90")
        removed = download_history.prune("auto", retention)
        if removed:
            log("info", f"下载历史清理：删除 {removed} 条（保留 {retention}）")
    except Exception as exc:  # noqa: BLE001 清理失败不影响主运行
        log("warn", f"下载历史清理失败：{exc}")
```

`lifespan` 内 `scheduler.start()` 之后：

```python
    await _prune_job()
    scheduler.add_daily("xiao_pan_prune", _prune_job)
```

- [ ] **Step 7: 前端保留策略与清理入口**

`frontend/src/api/types.ts` 的 `DownloadSettings` 加：

```ts
  history_retention: "days_30" | "days_90" | "days_180" | "forever";
```

`frontend/src/components/settings/SettingsDownload.vue` 的 `draft` 初始值补 `history_retention: "days_90"`，模板在"并发数"行后加：

```vue
    <div class="row">
      <label>历史保留</label>
      <el-select v-model="draft.history_retention" style="width: 200px">
        <el-option label="最近 30 天" value="days_30" />
        <el-option label="最近 90 天" value="days_90" />
        <el-option label="最近 180 天" value="days_180" />
        <el-option label="永久保留（手动清理）" value="forever" />
      </el-select>
      <span class="text-muted">自动清理过期下载记录，永久保留则只靠历史页的手动清理</span>
    </div>
```

`client.ts` 加：

```ts
  pruneDownloadHistory: (mode: "auto" | "failed" | "all") =>
    request<{ ok: boolean; removed: number }>("/api/downloads/history/prune", {
      method: "POST",
      body: JSON.stringify({ mode }),
    }),
```

`DownloadsView.vue` 历史 tab 的 `.filters` 末尾加清理下拉按钮：

```vue
          <el-dropdown @command="onPrune">
            <el-button size="small">清理</el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="auto">按保留策略清理</el-dropdown-item>
                <el-dropdown-item command="failed">只清失败记录</el-dropdown-item>
                <el-dropdown-item command="all" divided>清空全部记录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
```

```ts
const pruneLabels = { auto: "按保留策略", failed: "失败记录", all: "全部" } as const;

async function onPrune(mode: "auto" | "failed" | "all") {
  await ElMessageBox.confirm(`确认清理${pruneLabels[mode]}的下载历史？（不会删除磁盘文件）`, "清理历史", {
    type: "warning",
  });
  try {
    const r = await api.pruneDownloadHistory(mode);
    ElMessage.success(`已清理 ${r.removed} 条`);
    loadHistory();
  } catch (e) {
    ElMessage.error((e as Error).message);
  }
}
```

（`ElMessageBox` 已在 Task 5 的 import 中，若未 import 则补上。）

- [ ] **Step 8: 跑测试确认通过**

Run: `python3 -m pytest backend/tests -q && (cd frontend && npm run typecheck)`
Expected: 全绿。

- [ ] **Step 9: ruff + 提交**

```bash
python3 -m ruff check backend
git add backend/api/deps.py backend/api/routes_downloads.py backend/core/scheduler.py backend/main.py \
        backend/services/download_history.py backend/services/download_service.py backend/tests frontend/src
git commit -m "feat(download): 下载历史保留策略与自动/手动清理"
```

**禁止 `git add -A` / `git add .`**：工作区有未跟踪的 `data/`（含真实库）和 `.DS_Store`，绝不能进提交。

---

## Task 8: 文档与端到端验收

**Files:**
- Modify: `README.md`（下载/界面章节）

- [ ] **Step 1: 更新 README**

在"下载到本地"相关段落补：进行中/历史双 tab、历史支持状态+任务+关键词筛选与分页、`文件`列的到位校验含义（在/已丢失/未校验）、单文件重下行为（原路径重取直链，文件已在则跳过）、保留策略设置与每日清理；aria2 模式的终态由查询时对账回填，超过 24 小时未确认的记录判为失败。界面预览图说明同步补一句历史 tab。

- [ ] **Step 2: 端到端真机验收（逐条记录结果，明确区分已验证/未验证）**

后端：`python3 -m uvicorn backend.main:app --port 8000`；前端：`cd frontend && npm run dev`。

1. 内置模式跑一个带 `auto_download` 的任务 → 历史 tab 出现 `排队/下载中` → 完成后变 `完成`，`文件` 列显示「在」。
2. 设置切 aria2 模式（有 daemon 时投递，无 daemon 时确认自动降级并在历史留痕）→ 记录 `queued` → 对账后变 `done`。
3. 手动删掉已下载文件 → 刷新历史 → `文件` 列显示「已丢失」。
4. 对失败记录点「重下」→ 立即返回提示 → 刷新出现新记录 → 最终文件回到**原目标路径**（对照 `dest_path` 未变）。
5. `docker restart`（或重启 uvicorn）→ 历史记录仍在；重启前遗留的非终态记录按 24h 规则收口。
6. 设置保留策略为 `days_30` 后重启一次 → 日志出现"下载历史清理"（有可删数据时）。
7. 备份提示：动手前 `cp data/xiao_pan.db data/xiao_pan.db.bak`，验收用测试任务而非主力任务。

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs(readme): 补下载历史账本、到位校验与失败重下说明"
```

---

## 验收对照（spec → task）

| Spec 条款 | 实现任务 |
| --- | --- |
| 4.1 数据表 | Task 1 |
| 4.2 分层与 `download_history` | Task 2、3、4、7 |
| 4.3 API 契约（含路由顺序、`file_state` 线程池） | Task 3、4、6、7 |
| 4.4 非终态对账与 24h 收口 | Task 4 |
| 4.5 生命周期与写入点（写库失败不冒泡） | Task 2 |
| 4.6 `download_items()` 抽取与重下 | Task 6 |
| 4.7 双 tab 界面 | Task 5、6 |
| 4.8 保留策略与清理 | Task 7 |
| 2 节非目标（不做批量重下、不记前置失败、不后台轮询） | 全程不得出现对应代码 |
| 6 节测试 | 各 Task 的 Step 1 + Task 8 端到端 |
