# 任务执行形态（定时追更 / 仅手动 / 一次性）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给任务加三态执行形态 `run_mode`，让"仅手动"和"一次性"永不被定时器驱动，一次性任务真正跑完后自动停用，并修正全局「立即运行」会连停用任务一起跑的语义。

**Architecture:** 新增 `Task.run_mode` 一列（默认 `follow`，靠既有 `_auto_add_columns()` 自动补列，存量行为不变）。调度层集中两处改动：`main.apply_task_schedule` 按形态注册/撤销专属作业，`task_service` 的扫周期过滤加一环。收口判定依赖一处小重构——`_download_for_task` 从"把结果拼成通知文案就丢弃"改为返回结构化计数，供"新增+下载全成功"判定使用。UI 上「执行方式」进入表单第一屏，列表用 chips 表达三态与已完成。

**Tech Stack:** FastAPI + SQLModel/SQLite + APScheduler(AsyncIOScheduler)；Vue 3 `<script setup>` + Element Plus + Pinia；pytest（`asyncio_mode=auto`）+ ruff。

**Spec:** `docs/superpowers/specs/2026-10-05-task-run-modes-design.md`

## Global Constraints

- 解释器一律 `.venv/bin/python`（系统 `python3` 是 3.9 且无 pytest）。测试：`.venv/bin/python -m pytest backend/tests -q`；静态检查：`.venv/bin/python -m ruff check backend`；前端：`cd frontend && npm run typecheck && npm run build`。
- Python `>= 3.11`；ruff `line-length = 110`，`select = ["E","F","W","I","UP","B"]`，`ignore = ["E501"]`；**不新增任何依赖**。
- 注释、日志、通知、界面文案一律中文，沿用现有措辞风格（如「《任务名》已标记失效（…），跳过」）。
- `run_mode` 取值固定三个：`follow`（定时追更）/ `manual`（仅手动）/ `once`（一次性）。**非法值必须报错，绝不静默回退**——这是 spec 明确点名的反面教材（`schedule` 非法值现在会悄悄回退成每天跑）。
- 非法值的 HTTP 语义：Web 端点 → `400`，detail 逐字为 `执行方式只能是 follow / manual / once`；对外接口 → 沿用旧格式 `{"success": false, "code": 2, "message": ...}`。
- 存量任务默认 `follow`，**不写迁移脚本、不改写任何现有行**。
- 转存全局锁语义不变：转存在 `_run_lock` 内串行，下载仍在锁外（`task_service.py:180-197`）。
- 判定口径（spec 4.3）：`updated` 且（未开下载 → 完成；开了下载 → 实际执行且失败数为 0 → 完成）；`no_changes`、下载有失败、下载未实际执行 → 都不收口、保持启用。
- 测试共享一个临时库（`conftest.py` 隔离 `DATA_DIR`，且启动清理被 `XIAO_PAN_SKIP_STARTUP_PRUNE` 跳过）：**断言不得依赖全表数量或执行顺序**，用例自己 seed、自己清理。
- **绝不准碰 `data/`**（真实库 + 一个真实 3.5GB 媒体文件）与 `.DS_Store`；**禁止 `git add -A` / `git add .`**；**禁止 `git push`**（推送由用户手动执行）。
- 每个 Task 一次提交，中文 conventional commit。基线：**238 passed + 1 skipped**，必须保持绿。

---

## 文件结构

| 文件 | 本次职责 |
| --- | --- |
| `backend/models.py` | `RUN_MODES` 常量 + `Task.run_mode` 列（数据形状的唯一来源） |
| `backend/schemas.py` | `TaskIn` 透传 `run_mode` |
| `backend/api/routes_tasks.py` | Web 端点的 400 校验 |
| `backend/api/routes_external.py` | 油猴入口的默认值与旧格式报错 |
| `backend/main.py` | 按形态注册/撤销任务级作业 |
| `backend/services/task_service.py` | 扫周期跳过、`DownloadCounts` 结构化、一次性收口、批量跳过停用 |
| `frontend/src/api/types.ts` | `RunMode` 类型与 `Task` / `TaskPayload` 字段 |
| `frontend/src/components/TaskForm.vue` | 第一屏「执行方式」三选 + 条件收起 |
| `frontend/src/components/TaskRow.vue` | 三态与「已完成」徽标 |
| `README.md` | 三态语义、与停用的分工、批量运行新语义 |
| `backend/tests/test_task_run_mode.py` | 新建，覆盖形态字段/调度/收口/批量语义 |

---

## Task 1: 字段、校验与两条创建入口

**Files:**
- Modify: `backend/models.py`（在 `class Account` 之前加常量；`Task` 内 `schedule` 之后加列）
- Modify: `backend/schemas.py:28`（`TaskIn` 在 `schedule` 之后加字段）
- Modify: `backend/api/routes_tasks.py`（`create_task` / `update_task`）
- Modify: `backend/api/routes_external.py:95-115`（`add_task`）
- Test: `backend/tests/test_task_run_mode.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces：
  - `backend/models.py` → `RUN_MODES: tuple[str, str, str] = ("follow", "manual", "once")`
  - `Task.run_mode: str`（默认 `"follow"`）
  - `TaskIn.run_mode: str`（默认 `"follow"`）
  - `routes_tasks._check_run_mode(value: str) -> None`（非法抛 `HTTPException(400, "执行方式只能是 follow / manual / once")`）
  - Task 2/3/5 依赖的取值语义：`"follow"` 自动驱动、`"manual"` 与 `"once"` 不自动驱动

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_task_run_mode.py`：

```python
"""执行形态（run_mode）测试：字段往返、非法值 400、对外接口默认值。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete

from backend.database import session_scope
from backend.main import app
from backend.models import Account, ExternalApiToken, RUN_MODES, Task


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    """本模块自己建自己清，避免与同库其他模块互相干扰。"""
    for model in (Task, Account, ExternalApiToken):
        with session_scope() as s:
            s.exec(delete(model))
    yield
    for model in (Task, Account, ExternalApiToken):
        with session_scope() as s:
            s.exec(delete(model))


def _body(**extra):
    return {"taskname": "剧集", "shareurl": "https://pan.quark.cn/s/abc", "savepath": "/剧", **extra}


def test_default_run_mode_is_follow(client):
    created = client.post("/api/tasks", json=_body()).json()
    assert created["run_mode"] == "follow"


@pytest.mark.parametrize("mode", list(RUN_MODES))
def test_run_mode_roundtrip(client, mode):
    created = client.post("/api/tasks", json=_body(run_mode=mode)).json()
    assert created["run_mode"] == mode
    assert client.get("/api/tasks").json()[0]["run_mode"] == mode
    updated = client.put(f"/api/tasks/{created['id']}", json={**created, "run_mode": "once"}).json()
    assert updated["run_mode"] == "once"


def test_invalid_run_mode_rejected_on_create(client):
    resp = client.post("/api/tasks", json=_body(run_mode="sometimes"))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "执行方式只能是 follow / manual / once"


def test_invalid_run_mode_rejected_on_update(client):
    created = client.post("/api/tasks", json=_body()).json()
    resp = client.put(f"/api/tasks/{created['id']}", json={**created, "run_mode": "nope"})
    assert resp.status_code == 400


def test_payload_without_run_mode_stays_follow(client):
    """老调用方不传 run_mode 不能报错，按 follow 处理。"""
    created = client.post("/api/tasks", json=_body()).json()
    payload = {k: v for k, v in created.items() if k != "run_mode"}
    assert client.put(f"/api/tasks/{created['id']}", json=payload).json()["run_mode"] == "follow"


def test_external_add_task_defaults_to_follow_and_rejects_bad_value(client):
    token = client.post("/api/tokens", json={"name": "油猴"}).json()["token"]
    ok = client.post(
        f"/api/add_task?token={token}", json={"taskname": "a", "shareurl": "https://pan.quark.cn/s/a", "savepath": "/a"}
    ).json()
    assert ok["success"] is True
    assert client.get("/api/tasks").json()[0]["run_mode"] == "follow"

    bad = client.post(
        f"/api/add_task?token={token}",
        json={"taskname": "b", "shareurl": "https://pan.quark.cn/s/b", "savepath": "/b", "run_mode": "weekly"},
    ).json()
    assert bad == {"success": False, "code": 2, "message": "执行方式非法: weekly"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_task_run_mode.py -q`
Expected: FAIL —— `ImportError: cannot import name 'RUN_MODES'`（后续用例连带报错）。

- [ ] **Step 3: 加常量与列**

`backend/models.py`，在 `class Account` 之前：

```python
RUN_MODES: tuple[str, str, str] = ("follow", "manual", "once")  # 定时追更 / 仅手动 / 一次性
```

`Task` 内、紧跟 `schedule` 那行之后：

```python
    run_mode: str = "follow"  # follow=定时追更 | manual=仅手动 | once=一次性（跑完自动停用）
```

`backend/schemas.py` 的 `TaskIn` 末尾（`schedule` 之后）：

```python
    run_mode: str = "follow"
```

- [ ] **Step 4: Web 端点校验**

`backend/api/routes_tasks.py` 顶部 import 补 `RUN_MODES`（从 `..models`），在 `_to_out` 之后加：

```python
def _check_run_mode(value: str) -> None:
    """执行方式非法必须报错，不能像 schedule 那样静默回退成每天跑。"""
    if value not in RUN_MODES:
        raise HTTPException(400, f"执行方式只能是 {' / '.join(RUN_MODES)}")
```

`create_task` 里在构造 `Task` 之前加一行；`update_task` 里在写入字段之前加同一行（两处都读 `body.run_mode`）：

```python
        _check_run_mode(body.run_mode)
```

- [ ] **Step 5: 对外接口**

`backend/api/routes_external.py` 的 `add_task`：在必要字段校验之后、建 `Task` 之前插入，并把值传进构造：

```python
    run_mode = data.get("run_mode", "follow")
    if run_mode not in RUN_MODES:
        return {"success": False, "code": 2, "message": f"执行方式非法: {run_mode}"}
```

```python
            run_mode=run_mode,
```

`_old_format(task)` 保持原字段集不动（旧脚本的兼容面，本期不新增返回字段）。

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_task_run_mode.py -q`
Expected: 全绿（9 passed）。

- [ ] **Step 7: 全量 + 提交**

```bash
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m ruff check backend
git add backend/models.py backend/schemas.py backend/api/routes_tasks.py backend/api/routes_external.py backend/tests/test_task_run_mode.py
git commit -m "feat(tasks): 加执行形态 run_mode（follow/manual/once），非法值直接报错"
```

---

## Task 2: 调度层按形态驱动

**Files:**
- Modify: `backend/main.py:53-104`（`_run_one_task`、`apply_task_schedule`）
- Modify: `backend/services/task_service.py:135-155`（扫周期过滤链）
- Test: `backend/tests/test_task_run_mode.py`（追加）

**Interfaces:**
- Consumes: `Task.run_mode`（Task 1）
- Produces:
  - 规则：`run_mode != "follow"` 的任务 **既没有任务级作业，也不参与全局 sweep**；`trigger == "manual"` 的手动运行不受形态限制
  - `main.apply_task_schedule(task)` 的新行为：`disabled` 或 `run_mode != "follow"` → `unschedule_task`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_task_run_mode.py`。文件顶部的 import 段补齐（最终形态，不要留中间版本）：

```python
import asyncio
from types import SimpleNamespace  # 仅当需要时才用；本文件用真实 SavedFile，见 Task 3

from backend import main
from backend.core.engine import SavedFile, TaskRunResult
from backend.models import Account, ExternalApiToken, RUN_MODES, Task
from backend.services import task_service as ts
from backend.tests.test_task_service import OkDriver


class DownloadOkDriver(OkDriver):
    """带下载能力的假盘：test_task_service.OkDriver 的 capability 只有 rename。"""

    capability = {"rename", "download"}
    name = "假盘（可下载）"

    async def get_download_urls(self, fids):
        return (
            [{"fid": f, "file_name": "1.mp4", "size": 10, "download_url": f"http://d/{f}"} for f in fids],
            "ck=1",
        )
```

已核实的既有事实（省掉一步 wiring）：`routes_tasks.py:48-50` 与 `:67-69` 在**创建和更新之后都已经调用 `apply_task_schedule`**，所以形态变更会即时同步到调度器，本 Task 不需要改 API 层。

调度用例（每个用例末尾都要删自己建的行与作业，`with session_scope()` 删 `Task`，`scheduler.unschedule_task` 撤作业）：

```python
def _make_task(mode: str, **extra) -> int:
    with session_scope() as s:
        t = Task(
            taskname=f"形态{mode}", shareurl="https://pan.quark.cn/s/x", savepath="/x", run_mode=mode, **extra
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        return int(t.id)


def _reload_task(tid: int) -> Task:
    with session_scope() as s:
        return s.get(Task, tid)


def _drop(*ids: int) -> None:
    from backend.main import scheduler

    with session_scope() as s:
        for tid in ids:
            row = s.get(Task, tid)
            if row:
                s.delete(row)
    for tid in ids:
        scheduler.unschedule_task(tid)


def test_manual_and_once_do_not_register_task_job():
    from backend.main import scheduler

    ids = []
    try:
        for mode in ("manual", "once"):
            tid = _make_task(mode, schedule="interval:30")
            ids.append(tid)
            main.apply_task_schedule(_reload_task(tid))
            assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is None
    finally:
        _drop(*ids)


def test_follow_registers_and_switching_to_manual_cancels():
    from backend.main import scheduler

    tid = _make_task("follow", schedule="interval:30")
    try:
        main.apply_task_schedule(_reload_task(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is not None
        with session_scope() as s:
            row = s.get(Task, tid)
            row.run_mode = "manual"
            s.add(row)
        main.apply_task_schedule(_reload_task(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is None
    finally:
        _drop(tid)


def test_run_one_task_unschedules_itself_when_mode_changed():
    """定时器触发时若形态已改成非 follow，作业必须自我撤销，不能再驱动。"""
    from backend.main import scheduler

    tid = _make_task("follow", schedule="interval:30")
    try:
        main.apply_task_schedule(_reload_task(tid))
        with session_scope() as s:
            row = s.get(Task, tid)
            row.run_mode = "once"
            s.add(row)
        asyncio.run(main._run_one_task(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is None
    finally:
        _drop(tid)
```

扫周期跳过的两条：

```python
def _seed_account() -> int:
    with session_scope() as s:
        acc = Account(driver_key="fake", cookie="x", enabled=True, can_save=True, name="主号")
        s.add(acc)
        s.commit()
        s.refresh(acc)
        return int(acc.id)


@pytest.mark.asyncio
async def test_scheduled_sweep_skips_non_follow_modes(monkeypatch):
    acc_id = _seed_account()
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)
    ran: list[int] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(1)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    ids = [_make_task(m, account_id=acc_id) for m in ("manual", "once")]
    try:
        summary = await ts.run_tasks(trigger="scheduled")
        assert summary["skipped"] == 2 and summary["total"] == 2
        assert ran == []  # 一个都没被驱动
    finally:
        _drop(*ids)


@pytest.mark.asyncio
async def test_manual_trigger_still_runs_non_follow_modes(monkeypatch):
    acc_id = _seed_account()
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    tid = _make_task("manual", account_id=acc_id)
    try:
        summary = await ts.run_tasks(task_ids=[tid], trigger="manual")
        assert ran and summary["failed"] == 0
    finally:
        _drop(tid)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_task_run_mode.py -q`
Expected: FAIL —— `test_manual_and_once_do_not_register_task_job` 断言 `... is None` 失败（现在照旧注册），扫周期用例 `ran == []` 失败（现在被驱动）。

- [ ] **Step 3: 注册/撤销按形态**

`backend/main.py` 的 `apply_task_schedule` 改为（保留原有"行不存在/停用"分支）：

```python
    mode = getattr(row, "run_mode", "follow") or "follow"
    if row.disabled or mode != "follow":
        # 非 follow（仅手动/一次性）不该有任何定时器：撤销而非注册
        scheduler.unschedule_task(task.id)
        return
    scheduler.reschedule_task(task.id, getattr(row, "schedule", "") or "", partial(_run_one_task, task.id))
```

`_run_one_task` 的开头守卫同步加一环：

```python
    if row is None or row.disabled or getattr(row, "run_mode", "follow") != "follow":
        scheduler.unschedule_task(task_id)  # 已删/停用/改成非自动形态 → 撤销自身定时器
        return
```

- [ ] **Step 4: 全局 sweep 跳过**

`backend/services/task_service.py` 的 `_run_tasks_inner`，插在 `shareurl_ban` 检查之后、`has_valid_schedule` 检查之前：

```python
        # 仅手动 / 一次性：任何自动触发（全局 crontab 与任务级作业）都不驱动，只能手动点。
        if trigger == "scheduled" and getattr(task, "run_mode", "follow") != "follow":
            summary["skipped"] += 1
            tlog("info", f"《{task.taskname}》执行方式为 {task.run_mode}，不由定时器驱动")
            continue
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_task_run_mode.py -q`
Expected: 全绿。

- [ ] **Step 6: 全量 + ruff + 提交**

```bash
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m ruff check backend
git add backend/main.py backend/services/task_service.py backend/tests/test_task_run_mode.py
git commit -m "feat(tasks): 仅手动与一次性任务不注册定时器也不被全局扫周期驱动"
```

---

## Task 3: 下载结果结构化与一次性收口

**Files:**
- Modify: `backend/services/task_service.py:190-233`（`_download_for_task`、`updated` 分支）
- Test: `backend/tests/test_task_run_mode.py`（追加）

**Interfaces:**
- Consumes: `Task.run_mode`、`Task.auto_download`、`TaskRunResult.status`
- Produces:
  - `task_service.DownloadCounts`（dataclass：`attempted: int`、`ok: int`、`failed: int`、`executed: bool`）
  - `async _download_for_task(driver, task, result, settings, notify_lines, tlog, *, account_id=None) -> DownloadCounts`
  - `task_service._once_verdict(task, result, counts) -> tuple[bool, str]`（纯函数，便于逐组合断言）

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_task_run_mode.py`（import 用 Task 2 已建好的那批：`ts`、`SavedFile`、`TaskRunResult`、`DownloadOkDriver`，**并新增 `from backend.services.task_service import DownloadCounts`**）：

```python
def _once_task(**kw) -> Task:
    defaults = dict(taskname="一次性", shareurl="https://pan.quark.cn/s/x", savepath="/x", run_mode="once")
    defaults.update(kw)
    return Task(**defaults)


def _updated_result() -> TaskRunResult:
    return TaskRunResult(
        status="updated",
        files=[SavedFile(share_name="1.mp4", final_name="1.mp4", new_fid="f1", dest_path="/x/1.mp4")],
    )


@pytest.mark.parametrize(
    "auto_download,counts,status,want_done,want_reason_part",
    [
        (False, DownloadCounts(), "updated", True, "已获取新增资源"),
        (True, DownloadCounts(executed=True, attempted=2, ok=2), "updated", True, "已获取新增资源"),
        (True, DownloadCounts(executed=True, attempted=1, ok=1), "updated", True, "已获取新增资源"),
        (True, DownloadCounts(executed=True, attempted=2, ok=1, failed=1), "updated", False, "1 项下载失败"),
        (True, DownloadCounts(executed=False), "updated", False, "下载未实际执行"),
        (True, DownloadCounts(executed=True, attempted=0), "updated", False, "下载未实际执行"),
        (False, DownloadCounts(), "no_changes", False, "本次没有新增资源"),
        (True, DownloadCounts(executed=True, attempted=1, ok=1), "no_changes", False, "本次没有新增资源"),
        (False, DownloadCounts(), "failed", False, "转存未成功"),
    ],
)
def test_once_verdict(auto_download, counts, status, want_done, want_reason_part):
    task = _once_task(auto_download=auto_download)
    result = TaskRunResult(status=status) if status != "updated" else _updated_result()
    done, reason = ts._once_verdict(task, result, counts)
    assert done is want_done
    assert want_reason_part in reason


def test_once_verdict_skips_disabled_task_for_idempotency():
    """已停用（含此前自动完成）的一次性任务再手动跑一次，不重复收口、不重复通知。"""
    task = _once_task(auto_download=False, disabled=True)
    done, reason = ts._once_verdict(task, _updated_result(), DownloadCounts())
    assert not done and "停用" in reason


def test_once_verdict_ignores_non_once_tasks():
    task = _once_task(run_mode="follow", auto_download=False)
    done, reason = ts._once_verdict(task, _updated_result(), DownloadCounts())
    assert not done and reason == "非一次性任务"


@pytest.mark.asyncio
async def test_run_once_auto_disables_after_success(monkeypatch):
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=False)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        with session_scope() as s:
            assert s.get(Task, tid).disabled is True
    finally:
        _drop(tid)


@pytest.mark.asyncio
async def test_run_once_stays_enabled_when_download_failed(monkeypatch):
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=True)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    from backend.services import download_service

    async def fake_download(*a, **k):
        return ["✅ 1.mp4（1.0MB）", "❌ 2.mp4: HTTP 500"]

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    monkeypatch.setattr(download_service, "download_task_files", fake_download)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        with session_scope() as s:
            assert s.get(Task, tid).disabled is False
    finally:
        _drop(tid)


@pytest.mark.asyncio
async def test_run_once_stays_enabled_when_driver_cannot_download(monkeypatch):
    """开了下载但驱动不支持 → 下载未实际执行 → 不能算完成（防"看着完成了其实没下"）。"""
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=True)
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)  # 只有 rename 能力

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        with session_scope() as s:
            assert s.get(Task, tid).disabled is False
    finally:
        _drop(tid)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_task_run_mode.py -q -k "once or verdict"`
Expected: FAIL —— `ImportError: cannot import name 'DownloadCounts'`。

- [ ] **Step 3: 结构化计数**

`backend/services/task_service.py` 顶部 import 段加 `from dataclasses import dataclass`（该文件目前没有导入 dataclasses），并在 `DownloadCounts` 定义处：

```python
@dataclass
class DownloadCounts:
    """一次任务运行的下载结果计数：executed 表示是否真的走到了下载器。"""

    attempted: int = 0
    ok: int = 0
    failed: int = 0
    executed: bool = False
```

`_download_for_task` 改为返回计数（通知文案一行都不变）：

```python
async def _download_for_task(driver, task, result, settings, notify_lines, tlog, *, account_id=None) -> DownloadCounts:
    from .download_service import DownloadSettings, download_task_files

    counts = DownloadCounts()
    if not driver.has("download"):
        tlog("warn", f"《{task.taskname}》{driver.name} 驱动不支持下载，跳过")
        return counts
    cfg = DownloadSettings.from_dict(settings.get("download"))
    try:
        lines = await download_task_files(
            driver,
            result.files,
            cfg,
            download_subdir=bool(getattr(task, "download_subdir", False)),
            savepath_override=getattr(task, "download_savepath", "") or "",
            log=tlog,
            task_id=task.id,
            taskname=task.taskname,
            account_id=account_id,
        )
    except Exception as exc:  # noqa: BLE001 下载失败不影响转存结果
        tlog("error", f"《{task.taskname}》下载异常：{exc}")
        lines = [f"❌ 下载异常: {exc}"]
    counts.executed = True
    counts.attempted = len(lines)
    counts.ok = sum(1 for line in lines if line.startswith("✅"))
    counts.failed = counts.attempted - counts.ok
    if lines:
        notify_lines.append(f"📥《{task.taskname}》本地下载 {counts.ok}/{counts.attempted}：\n" + "\n".join(lines))
    return counts
```

- [ ] **Step 4: 一次性判定与收口**

同文件加纯函数（判定口径集中一处，spec 4.3 的表格逐条对应）：

```python
def _once_verdict(task, result, counts: DownloadCounts) -> tuple[bool, str]:
    """一次性任务是否算"跑完"：拿到新增资源，且（没开下载 或 下载实际执行且零失败）。"""
    if getattr(task, "run_mode", "follow") != "once":
        return False, "非一次性任务"
    if task.disabled:
        return False, "已在停用状态（含此前自动完成）"
    if result.status != "updated":
    if result.status != "updated":
        if result.status == "no_changes":
            return False, "本次没有新增资源"
        return False, f"转存未成功（{result.status}）"
    if getattr(task, "auto_download", False):
        if not counts.executed or counts.attempted == 0:
            return False, "下载未实际执行（驱动不支持或没有待下载文件）"
        if counts.failed:
            return False, f"{counts.failed} 项下载失败"
    return True, "已获取新增资源"


def _settle_once(task, result, counts: DownloadCounts, tlog) -> None:
    """判定通过才停用；停用后不再重复动作（幂等）。"""
    done, reason = _once_verdict(task, result, counts)
    if not done:
        if getattr(task, "run_mode", "follow") == "once" and not task.disabled:
            tlog("warn", f"《{task.taskname}》一次性任务未完成：{reason}，保持待执行")
        return
    with session_scope() as session:
        row = session.get(Task, task.id)
        if row is None or row.disabled:
            return
        row.disabled = True
        session.add(row)
    tlog("info", f"《{task.taskname}》一次性任务已完成并自动停用（{reason}）")
```

`updated` 分支里接住返回值并收口（下载仍在锁外，顺序不变）：

```python
            counts = DownloadCounts()
            if getattr(task, "auto_download", False):
                # 下载在锁外：本地/aria2 可与其它任务的转存并行，不占用转存串行段
                counts = await _download_for_task(
                    driver, task, result, settings, notify_lines, tlog, account_id=account.id
                )
            _settle_once(task, result, counts, tlog)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_task_run_mode.py -q`
Expected: 全绿（含 9 条参数化判定）。

- [ ] **Step 6: 全量 + ruff + 提交**

```bash
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m ruff check backend
git add backend/services/task_service.py backend/tests/test_task_run_mode.py
git commit -m "feat(tasks): 一次性任务按新增+下载全成功自动停用，未跑完保持待执行"
```

---

## Task 4: 全局「立即运行」跳过停用任务

**Files:**
- Modify: `backend/services/task_service.py:135-155`（过滤链最前）与 `run_tasks` 的 `summary`
- Test: `backend/tests/test_task_run_mode.py`（追加）

**Interfaces:**
- Consumes: `Task.disabled`
- Produces: `summary["disabled_skipped"]: int`（键名 Task 5 的前端汇总文案会引用），单行 `POST /api/tasks/{id}/run` 仍可跑停用行

- [ ] **Step 1: 写失败测试**

追加：

```python
@pytest.mark.asyncio
async def test_bulk_run_skips_disabled_tasks(monkeypatch):
    acc_id = _seed_account()
    dead = _make_task("follow", account_id=acc_id, disabled=True)
    live = _make_task("manual", account_id=acc_id)  # 仅手动 + 未停用 → 批量点应参与
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    try:
        summary = await ts.run_tasks(trigger="manual")  # 全局「立即运行」
        assert summary["disabled_skipped"] == 1
        assert summary["total"] == 2 and len(ran) == 1
    finally:
        _drop(dead, live)


@pytest.mark.asyncio
async def test_single_run_of_disabled_task_still_works(monkeypatch):
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, disabled=True)
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)
    called: list[int] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        called.append(1)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        assert called == [1]  # 行内「运行」是明确的手工意图，不被停用规则挡
    finally:
        _drop(tid)
```

`TaskRunResult` 与 `_drop` 都已在 Task 2/3 的 import 与助手里定义，**不要再写第二份清理助手**。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_task_run_mode.py -q -k "disabled or single_run"`
Expected: FAIL —— `KeyError: 'disabled_skipped'`（或第二条通过但第一条失败）。

- [ ] **Step 3: 过滤与计数**

`backend/services/task_service.py` 的 `run_tasks` 里 `summary` 初始化补一键：

```python
        "disabled_skipped": 0,
```

`_run_tasks_inner` 的循环里，插在 `shareurl_ban` 检查之后、形态检查之前：

```python
        # 全局/批量「立即运行」跳过停用任务：停用=暂停一切。行内单个「运行」按钮例外
        # （task_ids 非空即用户明确指定了这一行），便于手工重试已完成的一次性任务。
        if task.disabled and task_ids is None:
            summary["disabled_skipped"] += 1
            tlog("info", f"《{task.taskname}》已停用，批量运行跳过")
            continue
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_task_run_mode.py -q`
Expected: 全绿。

- [ ] **Step 5: 全量 + ruff + 提交**

```bash
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m ruff check backend
git add backend/services/task_service.py backend/tests/test_task_run_mode.py
git commit -m "fix(tasks): 批量立即运行跳过停用任务，行内运行仍可手工重试"
```

---

## Task 5: 表单「执行方式」、列表徽标与文档

**Files:**
- Modify: `frontend/src/api/types.ts`（`RunMode` + `Task` / `TaskPayload`）
- Modify: `frontend/src/components/TaskForm.vue:38-49,184-246,330-355`
- Modify: `frontend/src/components/TaskRow.vue:13-27`（chips）
- Modify: `README.md`
- Test: `npm run typecheck` + `npm run build` + 真机点击（本仓库无前端单测框架）

**Interfaces:**
- Consumes: `run_mode`（Task 1）、`disabled` 语义与批量跳过规则（Task 4）
- Produces: 用户可见的三态选择与「已完成」徽标；无新接口

- [ ] **Step 1: 类型**

`frontend/src/api/types.ts` 在 `Task` 定义之前加：

```ts
export type RunMode = "follow" | "manual" | "once";
```

`Task` 与 `TaskPayload` 各加一行（放在 `schedule` 之后）：

```ts
  run_mode: RunMode;
```

- [ ] **Step 2: 表单第一屏**

`TaskForm.vue` 的 `blank()` 里 `auto_download: true` 之后加：

```ts
    run_mode: "follow" as RunMode,
```

`<script setup>` 里 import 补 `RunMode`，并加：

```ts
const RUN_MODE_OPTIONS: { value: RunMode; label: string; hint: string }[] = [
  { value: "follow", label: "定时追更", hint: "按更新频率/全局调度自动追更" },
  { value: "manual", label: "仅手动", hint: "永不自动跑，只在点「运行」时执行" },
  { value: "once", label: "一次性", hint: "只手动跑；拿到新增且下载全部成功后自动停用" },
];
const isFollow = computed(() => draft.run_mode === "follow");
```

模板里在「更新频率」那一栏（`<div class="f f--wide">` 起于「更新频率」label）**之前**插入：

```vue
      <div class="f f--wide">
        <label class="field-label">执行方式</label>
        <el-radio-group v-model="draft.run_mode">
          <el-radio-button v-for="o in RUN_MODE_OPTIONS" :key="o.value" :value="o.value">{{ o.label }}</el-radio-button>
        </el-radio-group>
        <div class="hint">{{ RUN_MODE_OPTIONS.find((o) => o.value === draft.run_mode)?.hint }}</div>
      </div>
```

把「更新频率」整栏包进条件（`v-if="isFollow"`，保持原有 `f f--wide` 结构不变）；高级区里的「截止日期」「按星期运行」两栏同样加 `v-if="isFollow"`。并在非 follow 时加一句提示，放在执行方式 hint 之后：

```vue
        <div v-if="!isFollow && draft.disabled" class="hint">
          该行当前为停用状态；改回「定时追更」后需手动取消停用才会恢复自动运行。
        </div>
```

- [ ] **Step 3: 列表徽标**

`TaskRow.vue` 的 `chips` 计算里，在现有 `t.pattern` 之前插入（顺序：形态优先于细节配置）：

```ts
  if (t.run_mode === "manual") list.push({ key: "m", text: "仅手动" });
  if (t.run_mode === "once" && !t.disabled) list.push({ key: "o", text: "一次性待执行", primary: true });
  if (t.run_mode === "once" && t.disabled) list.push({ key: "done", text: "已完成", primary: true });
  if (t.run_mode === "follow" && t.schedule) list.push({ key: "s", text: `频率 ${t.schedule}` });
```

原有那条 `t.startfid` 的 `key: "s"` 改成 `key: "sf"`（`v-for` 的 `:key` 必须唯一）。

- [ ] **Step 4: 类型检查与构建**

```bash
cd frontend && npm run typecheck && npm run build
```

Expected: 0 错误，build 成功（>500kB chunk 警告为既有）。

- [ ] **Step 5: 真机验证（必须实际点，不能只看类型）**

后端用 `.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000` 配合**临时 `DATA_DIR`**（把 `data/xiao_pan.db` 复制到 `/tmp/xiao-pan-runmode-check/` 下，并在临时库里把 `crontab` 设成远期、`sign_enabled`/`notify_enabled` 设 false、任务 `disabled=1`，避免任何真实转存或推送）；前端 `npm run dev`。逐条验证并记录：

1. 新建任务时第一屏能看到「执行方式」，默认「定时追更」；切到「仅手动」后「更新频率」消失、切回又出现。
2. 保存 `manual` 任务后，列表出现灰「仅手动」徽标。
3. 保存 `once` 任务后是蓝「一次性待执行」；行内「运行」按钮仍可用。
4. 把临时库里某个任务改成 `once` + `disabled=1`，刷新后徽标显示绿「已完成」而不是「停用」。
5. 「全部立即运行」跳过停用行，日志区出现「已停用，批量运行跳过」。
6. 停用的行用行内「▶ 运行」仍能跑（日志有「开始运行」）。
7. 收尾：杀掉自己起的进程，删掉 `/tmp/xiao-pan-runmode-check`，确认 `data/` 未被写入（比对前后 md5）。

如果环境无法完成某条，明确写「NOT VERIFIED + 原因」。

- [ ] **Step 6: README**

在任务管理/调度段落补三点，中文，简短：

```markdown
- 🎛 **执行方式三态**：**定时追更**（默认，按频率或全局 crontab 自动跑）/ **仅手动**（永不自动触发）/
  **一次性**（手动跑；拿到新增且本地下载全部成功后**自动停用**，列表显示「已完成」）。
  分工：**执行方式决定要不要自动跑，「停用」决定要不要临时暂停一切**。
- 🚫 **批量「立即运行」跳过已停用任务**（汇总里给出跳过条数）；行内「▶ 运行」对停用行仍可用，便于手工重试。
```

- [ ] **Step 7: 提交**

```bash
git add frontend/src/api/types.ts frontend/src/components/TaskForm.vue frontend/src/components/TaskRow.vue README.md
git commit -m "feat(ui): 表单第一屏可选执行方式，列表显示仅手动/一次性/已完成徽标"
```

---

## 验收对照（spec → task）

| Spec 条款 | 实现任务 |
| --- | --- |
| 4.1 `run_mode` 字段、非法值 400、存量默认 follow 不迁移 | Task 1 |
| 4.2 不注册作业、撤销旧作业、全局 sweep 跳过、手动入口不受限 | Task 2 |
| 4.3 六种收口组合、幂等、已知「已完成」显示瑕疵 | Task 3（判定）+ Task 5（显示） |
| 4.4 批量跳过 `disabled`、单行仍可跑、跳过计数 | Task 4 |
| 4.5 `_download_for_task` 返回结构化计数 | Task 3 |
| 4.6 API 契约与对外接口默认值 | Task 1 |
| 4.7 表单「执行方式」+ 条件收起 + 徽标 | Task 5 |
| 5 测试 | 各 Task Step 1 + Task 5 Step 5 |
| 2 非目标（不归档隐藏、不加第四状态位、不自动取消停用） | 全程不得出现对应代码 |
