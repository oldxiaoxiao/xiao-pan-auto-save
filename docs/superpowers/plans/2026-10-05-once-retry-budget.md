# 一次性任务的重试预算 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `once`（一次性）任务自动重试到真正拿到资源为止，`no_changes` 不占预算，真失败最多三次、每次间隔 5 分钟，用尽后停摆等用户点「▶ 运行」重新开启。

**Architecture:** 两列新数据（`retry_attempts`、`next_retry_at`）+ 一个**唯一判定函数** `once_next_driver(task)` 返回该行的驱动方式（`retry`/`daily`/`halted`/`none`），调度注册、全局扫周期、结局写库三处都只问它，不再各写一遍条件。重试用 APScheduler `DateTrigger` 一次性作业；`next_retry_at` 落库是为了进程重启后能从数据重建作业（JobStore 是内存的）。

**Tech Stack:** FastAPI + SQLModel/SQLite + APScheduler(AsyncIOScheduler)；Vue 3 `<script setup>` + Element Plus；pytest（`asyncio_mode=auto`）+ ruff。

**Spec:** `docs/superpowers/specs/2026-10-05-once-retry-budget-design.md`（并修订 `docs/superpowers/specs/2026-10-05-task-run-modes-design.md` 的 4.2/4.3）

## Global Constraints

- 解释器一律 `.venv/bin/python`。测试 `.venv/bin/python -m pytest backend/tests -q`；lint `.venv/bin/python -m ruff check backend`；前端 `cd frontend && npm run typecheck && npm run build`。
- Python >= 3.11；ruff `line-length = 110`，`select = ["E","F","W","I","UP","B"]`，`ignore = ["E501"]`；**不新增依赖**（cron 解析用现成的 APScheduler）。
- 常量逐字：`ONCE_RETRY_LIMIT = 3`、`ONCE_RETRY_DELAY_MINUTES = 5`。**首档也是 5 分钟**（用户明确要求，1 分钟低于表单自标的风控提示）。
- 语义铁律：`no_changes`（还没放出）**不占预算**；只有真失败（`failed`/`network`/`banned`，或 `updated` 但下载有失败）才 `+1`。
- **停摆不写 `disabled`**：`disabled` 仍只表示"用户暂停"或"已完成"。
- 所有 `run_mode` 读取必须走 `run_mode_of(task)`（`backend/models.py`）；新两列在存量行上分别是 `0` / `NULL`（`_auto_add_columns()` 行为），无需归一化，但**必须有升级测试**。
- 旁路写库（计数、时间戳、收口）失败只 `log("warn", ...)`，**绝不**抛回运行循环砍掉整批或吞通知。
- 转存段仍在 `_run_lock` 内串行、下载在锁外；不改。
- 注释/日志/界面文案中文。测试共享临时库：按自建 id 断言，不看全表计数、不依赖执行顺序。
- **绝不碰 `data/`**（真实库 + 3.5GB 媒体文件）；**绝不 `git add -A` / `git add .`**；**绝不 `git push`**。工作目录里另有会话在改 DLNA（`docker-compose.yml`/`.dockerignore`/`deploy/`/`Dockerfile.minidlna` 以及 **`README.md` 的未提交改动**）：那几样**不碰不提交**；本次唯一需要写进 `README.md` 的一句话按 Task 4 Step 3 的 hunk 规则处理。
- 基线：**289 passed + 1 skipped**；每任务一次中文 conventional commit。

---

## 文件结构

| 文件 | 本次职责 |
| --- | --- |
| `backend/models.py` | `Task.retry_attempts` / `Task.next_retry_at` 两列 |
| `backend/core/scheduler.py` | `enddate_passed(task, today)` 助手（`task_due_today` 复用它，不重复解析日期）；`reschedule_retry_at(task_id, when, func)` / `unschedule_retry(task_id)`：`DateTrigger` 一次性作业 |
| `backend/services/task_service.py` | 常量 + `once_next_driver` + `scheduled_should_run` + 结局写库 + `reset_once_budget` |
| `backend/main.py` | `apply_task_schedule` 分流 / `_run_retry_task` / `reschedule_all_tasks` 接入 |
| `backend/api/routes_tasks.py` | 行内「▶ 运行」前置归零预算；`TaskOut` 投影 |
| `backend/schemas.py` | `TaskOut` 暴露两个新字段 |
| `frontend/src/api/types.ts`、`components/TaskRow.vue`、`components/TaskForm.vue` | 徽标四态 + tooltip + 「执行方式」文案 |
| `backend/tests/test_once_retry_budget.py` | 新建，本特性的全部测试 |
| `docs/superpowers/specs/2026-10-05-task-run-modes-design.md` | 修订注记；`README.md` 只在无未提交改动时才动（见 Task 4 Step 3） |

---

## Task 1: 数据列与唯一判定函数

**Files:**
- Modify: `backend/models.py`（`Task` 内、`schedule` 之后）
- Modify: `backend/core/scheduler.py`（`enddate_passed`，并让 `task_due_today` 调它）
- Modify: `backend/services/task_service.py`（常量 + `once_next_driver` + `scheduled_should_run`）
- Test: `backend/tests/test_once_retry_budget.py`（新建）

**Interfaces:**
- Consumes: `run_mode_of`（既有）
- Produces:
  - `Task.retry_attempts: int`（默认 0）、`Task.next_retry_at: NaiveDatetime | None`（默认 None）
  - `backend/core/scheduler.py` → `enddate_passed(task, today: date | None = None) -> bool`
  - `backend/services/task_service.py` → `ONCE_RETRY_LIMIT = 3`、`ONCE_RETRY_DELAY_MINUTES = 5`
  - `once_next_driver(task) -> str` ∈ `"retry" | "daily" | "halted" | "none"`
  - `scheduled_should_run(task) -> tuple[bool, str]`

> spec 4.1 里写的 `once_next_driver(task, now=None)` 在计划阶段砍掉了 `now`：判"到点时间是否已过期"属于 spec 2 的非目标，留着就是无人读取的死参数。spec 那行随之作废，Task 4 Step 3 的修订注记里一并说明。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_once_retry_budget.py`：

```python
"""一次性任务的重试预算：驱动判定、预算消耗、停摆与重启恢复。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.core.scheduler import enddate_passed
from backend.models import Task
from backend.services.task_service import (
    ONCE_RETRY_DELAY_MINUTES,
    ONCE_RETRY_LIMIT,
    once_next_driver,
    scheduled_should_run,
)


def _t(**kw) -> Task:
    base = dict(taskname="一次性", shareurl="https://pan.quark.cn/s/x", savepath="/x", run_mode="once")
    base.update(kw)
    return Task(**base)


def test_constants_match_spec():
    assert ONCE_RETRY_LIMIT == 3
    assert ONCE_RETRY_DELAY_MINUTES == 5


def test_non_once_tasks_are_not_governed_by_the_budget():
    assert once_next_driver(_t(run_mode="follow")) == "none"
    assert once_next_driver(_t(run_mode="manual")) == "none"
    assert once_next_driver(_t(run_mode="")) == "none"  # 老库升级出来的空串按 follow 读


def test_once_driver_states():
    # 待执行：没有预算消耗、也没有到点时间 → 由每日扫驱动
    assert once_next_driver(_t()) == "daily"
    # 有到点时间且预算未用尽 → 由一次性重试作业驱动（每日扫必须让位，避免双驱动）
    assert once_next_driver(_t(retry_attempts=1, next_retry_at=datetime.now() + timedelta(minutes=5))) == "retry"
    # 预算用尽且无到点时间 → 停摆
    assert once_next_driver(_t(retry_attempts=ONCE_RETRY_LIMIT)) == "halted"
    # 已完成（自动停用） → 停摆
    assert once_next_driver(_t(disabled=True, retry_attempts=0)) == "halted"
    # 已过截止日期 → 停摆
    assert once_next_driver(_t(enddate="2020-01-01")) == "halted"


def test_exhausted_budget_wins_over_a_lingering_retry_slot():
    """矛盾态（预算已用尽却还留着到点时间）必须判 halted。

    正常写库路径不会产出这个组合（用尽时把 next_retry_at 置 None），但手工改库、
    或老库里残留一列非空值都会留下它。判序反过来就等于给停摆的行复活一条命。
    """
    assert once_next_driver(_t(retry_attempts=ONCE_RETRY_LIMIT, next_retry_at=datetime.now() + timedelta(minutes=5))) == "halted"


def test_budget_is_never_consumed_by_no_release():
    """判定函数本身不改状态；这里钉的是"没放出/等重试"与"该跑"的映射。"""
    ok, why = scheduled_should_run(_t())
    assert ok and why == ""
    ok, why = scheduled_should_run(_t(retry_attempts=2, next_retry_at=datetime.now() + timedelta(minutes=5)))
    assert not ok and "重试" in why
    ok, why = scheduled_should_run(_t(retry_attempts=ONCE_RETRY_LIMIT))
    assert not ok and "用尽" in why
    ok, why = scheduled_should_run(_t(run_mode="manual"))
    assert not ok and "仅手动" in why
    ok, why = scheduled_should_run(_t(run_mode="follow"))
    assert ok and why == ""


def test_enddate_passed_helper_and_illegal_value_does_not_block():
    assert enddate_passed(_t(enddate="2020-01-01"), today=datetime(2026, 10, 5).date()) is True
    assert enddate_passed(_t(enddate="2099-01-01"), today=datetime(2026, 10, 5).date()) is False
    assert enddate_passed(_t(enddate=""), today=None) is False
    assert enddate_passed(_t(enddate="非法日期"), today=None) is False  # 与 task_due_today 同口径：不挡路


def test_task_out_proxies_the_two_new_fields():
    """前端只认 TaskOut 的投影：datetime 必须变成字符串，否则 JSON 序列化直接 500。

    `id=1` 不是随手给的：`TaskOut.id: int` 是必填，未落库的 `Task(id=None)` 会在构造 TaskOut 时就报错。
    """
    from backend.api.routes_tasks import _to_out

    when = datetime(2026, 10, 5, 19, 30)
    out = _to_out(_t(id=1, retry_attempts=2, next_retry_at=when))
    assert out.retry_attempts == 2 and out.next_retry_at == when.isoformat()
    blank = _to_out(_t(id=1))
    assert blank.retry_attempts == 0 and blank.next_retry_at is None


def test_auto_add_columns_gives_existing_rows_safe_defaults(monkeypatch):
    """真·老库升级回归（spec 5）：拿一张**没有这两列**的老 schema 表跑 `_auto_add_columns()`，
    存量行必须补出 `0` / `NULL`，且原本正常追更的 follow 行不能因为补列被误停（`run_mode` 那次踩过的坑）。

    为什么不能改用共享测试库"省略列 INSERT"来复刻：那份库是 `init_db()` 按新模型 `create_all` 出来的，
    `retry_attempts` 是 `INTEGER NOT NULL` 而**没有** server 端默认值（SQLModel 的 python 默认不进 DDL），
    省略该列的 INSERT 会直接撞约束。只有走 `ALTER ADD COLUMN ... NOT NULL DEFAULT 0` 的真升级才有库侧默认，
    所以这里另起一个内存库，把模块全局 engine 临时换掉。
    """
    from sqlalchemy import create_engine, select, text
    from sqlalchemy.pool import StaticPool

    import backend.database as db
    from backend.models import Task

    old = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with old.begin() as c:
        c.execute(text(
            "CREATE TABLE task (id INTEGER PRIMARY KEY, taskname VARCHAR NOT NULL, shareurl VARCHAR NOT NULL, "
            "savepath VARCHAR NOT NULL, run_mode VARCHAR NOT NULL, created_at DATETIME NOT NULL)"
        ))
        c.execute(text("INSERT INTO task (taskname, shareurl, savepath, run_mode, created_at) "
                       "VALUES ('存量追更行', 'u', '/x', 'follow', '2026-01-01 00:00:00')"))
        c.execute(text("INSERT INTO task (taskname, shareurl, savepath, run_mode, created_at) "
                       "VALUES ('存量一次性行', 'u', '/x', 'once', '2026-01-01 00:00:00')"))
    monkeypatch.setattr(db, "engine", old)  # _auto_add_columns 与 session_scope 都在调用时读这个模块全局名
    try:
        db._auto_add_columns()
        with db.session_scope() as s:
            follow = s.exec(select(Task).where(Task.taskname == "存量追更行")).one()
            once = s.exec(select(Task).where(Task.taskname == "存量一次性行")).one()
            for row in (follow, once):
                assert row.retry_attempts == 0 and row.next_retry_at is None
            assert scheduled_should_run(follow) == (True, "")  # 老追更行不能停更
            assert once_next_driver(once) == "daily"  # 补列后也不能一上来就停摆
    finally:
        old.dispose()
```

`_auto_add_columns` 会把老表里缺的**所有**列一起补上（`pattern` 等 → `NOT NULL DEFAULT ''`），正是真升级的形状；共享测试库全程不参与，不会串扰别的模块。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_once_retry_budget.py -q`
Expected: FAIL —— `ImportError: cannot import name 'enddate_passed'` / `cannot import name 'once_next_driver'`。

- [ ] **Step 3: 两列**

`backend/models.py` 的 `Task`，紧跟 `schedule` 那行：

```python
    retry_attempts: int = 0  # 一次性任务本轮已消耗的重试次数（成功或手动再开时归零）
    next_retry_at: NaiveDatetime | None = None  # 到点重试时间；内存 JobStore 重启会丢，故必须落库
```

`TaskIn` 不加这两列（用户不该手填），但 `TaskOut` 要暴露（见 Step 6）。

- [ ] **Step 4: 日期助手（并让既有判定复用）**

`backend/core/scheduler.py`：

```python
def enddate_passed(task, today: date | None = None) -> bool:
    """截止日期是否已过；空或非法日期一律 False（不挡路），与 task_due_today 既有口径一致。"""
    enddate = getattr(task, "enddate", "") or ""
    if not enddate:
        return False
    try:
        return (today or date.today()) > datetime.strptime(enddate, "%Y-%m-%d").date()
    except ValueError:
        return False
```

把 `task_due_today` 里原有的 `enddate` 解析块换成调用它：

```python
    if enddate_passed(task, today):
        return False
```

- [ ] **Step 5: 唯一判定函数**

`backend/services/task_service.py`：第 16 行改成
`from ..core.scheduler import enddate_passed, has_valid_schedule, task_due_today`
（`enddate_passed` 在 `core/scheduler.py`，**不是** `models`；第 17 行 `from ..database import session_scope` 已有，Step 3 的写库要用它）。

```python
ONCE_RETRY_LIMIT = 3  # 真失败最多重试三次，用尽后停摆等手动「▶ 运行」
ONCE_RETRY_DELAY_MINUTES = 5  # 三档都是 5 分钟：1 分钟低于表单自标的「建议 ≥5 分钟」风控线


def once_next_driver(task) -> str:
    """一次性任务下一步由谁驱动：retry / daily / halted / none。

    调度注册、全局扫周期、结局写库三处都只问这个函数 —— 判定散在两处是上个特性踩过的漂移源。
    判序是刻意的：预算检查必须排在 next_retry_at 之前，否则手工改库留下的矛盾态
    （用尽 + 还挂着到点时间）会被判成 retry，等于给本该停摆的行复活一条命。
    """
    if run_mode_of(task) != "once":
        return "none"
    if task.disabled:
        return "halted"  # 已完成或用户暂停，都不再自动驱动
    if enddate_passed(task):
        return "halted"
    if int(getattr(task, "retry_attempts", 0) or 0) >= ONCE_RETRY_LIMIT:
        return "halted"  # 预算用尽：停摆，等手动点运行重新给预算
    if getattr(task, "next_retry_at", None) is not None:
        return "retry"
    return "daily"


def scheduled_should_run(task) -> tuple[bool, str]:
    """定时触发（全局 crontab 或任务级作业）该不该驱动这一行，以及不驱动的原因。"""
    mode = run_mode_of(task)
    if mode == "follow":
        return True, ""
    if mode == "manual":
        return False, "仅手动"
    driver = once_next_driver(task)
    if driver == "daily":
        return True, ""
    if driver == "retry":
        return False, "等到点重试作业驱动，每日扫不让位就会双驱动"
    if int(getattr(task, "retry_attempts", 0) or 0) >= ONCE_RETRY_LIMIT and not task.disabled:
        return False, "重试已用尽"
    return False, "已完成或已停用"
```

- [ ] **Step 6: `TaskOut` 暴露**

`backend/schemas.py` 的 `TaskOut`（`shareurl_ban` 附近）加：

```python
    retry_attempts: int = 0
    next_retry_at: str | None = None
```

`backend/api/routes_tasks.py` 的 `_to_out`（第 22-28 行）已有 `data = task.model_dump()` + 归一化 `run_mode`；紧跟 `last_run_at` 那行后面补同风格的字符串化，别引入第二套格式化逻辑：

```python
    data["next_retry_at"] = task.next_retry_at.isoformat() if task.next_retry_at else None
```

- [ ] **Step 7: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_once_retry_budget.py -q`
Expected: 全绿。

- [ ] **Step 8: 全量 + ruff + 提交**

```bash
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m ruff check backend
git add backend/models.py backend/core/scheduler.py backend/services/task_service.py backend/schemas.py backend/api/routes_tasks.py backend/tests/test_once_retry_budget.py
git commit -m "feat(tasks): 一次性任务加重试预算与到点时间，驱动判定收敛成单一函数"
```

---

## Task 2: 调度接入（到点重试作业 + 每日扫 + 重启恢复）

**Files:**
- Modify: `backend/core/scheduler.py`（`reschedule_retry_at`）
- Modify: `backend/main.py`（`_run_retry_task`、`apply_task_schedule`、`reschedule_all_tasks`）
- Modify: `backend/services/task_service.py`（扫周期改用 `scheduled_should_run`）
- Test: `backend/tests/test_once_retry_budget.py`（追加）

**Interfaces:**
- Consumes: `once_next_driver` / `scheduled_should_run`（Task 1）
- Produces:
  - `TaskScheduler.reschedule_retry_at(task_id: int, when: datetime, func) -> str`
  - `main._run_retry_task(task_id: int) -> None`
  - 作业 id 约定：任务级周期作业仍是 `xiao_pan_task_{id}`，重试作业是 `xiao_pan_retry_{id}`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_once_retry_budget.py`。文件顶部把导入补全（`_seed_account` / `_drop_account` / `DownloadOkDriver` **一律从 `test_task_run_mode` 复用**，`backend.tests.test_task_service` 里的 `OkDriver` 已经被它继承过，别在两个文件里各写一遍假盘；`backend.models` 那行是把 Task 1 已有的 `from backend.models import Task` 扩成两个名字，不是再导一次）：

```python
import asyncio

from backend import main
from backend.core.engine import SavedFile, TaskRunResult
from backend.database import session_scope
from backend.main import app, scheduler
from backend.models import Account, Task  # 覆盖 Task 1 的 import 行，别留两行
from backend.services import task_service as ts
from backend.tests.test_task_run_mode import DownloadOkDriver, _drop_account, _seed_account


@pytest.fixture(scope="module")
def client():
    """本模块自己要打 HTTP 端点；照 test_task_run_mode 的写法开 TestClient。"""
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    """本模块自建自清：`run_tasks(trigger="scheduled")` 不带 task_ids 时会载入全表，
    别的环境里残留的行会让「只有这一行进了引擎」这类断言变成顺序敏感的随机失败。
    """
    from sqlmodel import delete

    for model in (Task, Account):
        with session_scope() as s:
            s.exec(delete(model))
    yield
    for model in (Task, Account):
        with session_scope() as s:
            s.exec(delete(model))


def _persist(**kw) -> int:
    with session_scope() as s:
        t = _t(**kw)
        s.add(t)
        s.commit()
        s.refresh(t)
        return int(t.id)


def _reload(tid: int) -> Task:
    with session_scope() as s:
        return s.get(Task, tid)


def _delete(*ids: int) -> None:
    """删行必须连作业一起撤：DateTrigger 作业在调度器停止时只是躺在 pending 里，
    下一个开 TestClient 的用例一 start 就会真的把它跑起来，串扰到别人的断言。
    """
    with session_scope() as s:
        for tid in ids:
            row = s.get(Task, tid)
            if row:
                s.delete(row)
    for tid in ids:
        main.scheduler.unschedule_task(tid)
        main.scheduler.unschedule_retry(tid)


def _naive_run_date(job) -> datetime:
    """DateTrigger 会把 naive 时间补成本地时区（apscheduler/triggers/date.py），
    直接拿它减 naive 的 `when` 会抛 TypeError，所以先剥回 naive。
    """
    run_date = job.trigger.run_date
    return run_date.replace(tzinfo=None) if run_date.tzinfo else run_date
```

`main.py` 里 `task_service` 只在函数内导入（第 35、73 行），**没有**模块级属性，所以打补丁必须打在 `ts.run_tasks` 上（`backend.services.task_service` 模块对象），别写 `main.task_service`。

作业断言用例（`scheduler` 未 start 时 `add_job` 只进 pending，`get_job` 仍能看到 —— 口径同 `test_scheduler_log.py`，所以这里断言 `trigger` 而不猜 `next_run_time`）：

```python
def test_once_with_retry_due_registers_a_one_shot_job():
    from apscheduler.triggers.date import DateTrigger

    when = datetime.now() + timedelta(minutes=5)
    tid = _persist(retry_attempts=1, next_retry_at=when)
    try:
        main.apply_task_schedule(_reload(tid))
        job = scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}")
        assert job is not None and isinstance(job.trigger, DateTrigger)
        assert abs((_naive_run_date(job) - when).total_seconds()) < 5
        assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is None  # 不挂周期作业
    finally:
        _delete(tid)


def test_retry_job_is_cleared_when_budget_exhausted_or_disabled():
    tid = _persist(retry_attempts=3, next_retry_at=datetime.now() + timedelta(minutes=5))
    try:
        main.apply_task_schedule(_reload(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}") is None
    finally:
        _delete(tid)


def test_reschedule_all_rebuilds_pending_retry_after_restart():
    """内存 JobStore 重启即空，重启恢复必须完全靠库里的 next_retry_at。"""
    tid = _persist(retry_attempts=2, next_retry_at=datetime.now() + timedelta(minutes=5))
    try:
        main.apply_task_schedule(_reload(tid))
        scheduler.unschedule_retry(tid)
        assert scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}") is None
        main.reschedule_all_tasks()
        assert scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}") is not None
    finally:
        _delete(tid)


def test_ordinary_once_task_gets_no_task_level_job():
    """没排到点时间的一次性任务只靠每日扫，不能顺手给它挂个周期作业。"""
    tid = _persist()
    try:
        main.apply_task_schedule(_reload(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is None
        assert scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}") is None
    finally:
        _delete(tid)


@pytest.mark.asyncio
async def test_retry_job_run_clears_the_slot_before_running(monkeypatch):
    """到点先清 next_retry_at 再跑：否则判定会把这轮当成"等重试"而自我跳过。"""
    seen: dict[str, object] = {}

    async def fake_run(task_ids=None, trigger="manual"):
        row = _reload(task_ids[0])
        seen["next_retry_at"] = row.next_retry_at
        seen["trigger"] = trigger
        return {"run_id": "x"}

    monkeypatch.setattr(ts, "run_tasks", fake_run)
    tid = _persist(retry_attempts=1, next_retry_at=datetime.now() - timedelta(minutes=1))
    try:
        await main._run_retry_task(tid)
        assert seen["next_retry_at"] is None and seen["trigger"] == "scheduled"
        assert _reload(tid).next_retry_at is None
    finally:
        _delete(tid)


@pytest.mark.asyncio
async def test_retry_job_run_on_deleted_task_is_silent(monkeypatch):
    """作业躺在内存里、行已被删：直接返回，既不抛异常也不凭空造出一次运行。"""
    called: list[int] = []

    async def fake_run(task_ids=None, trigger="manual"):
        called.append(task_ids[0])
        return {"run_id": "x"}

    monkeypatch.setattr(ts, "run_tasks", fake_run)
    await main._run_retry_task(999999)  # 库里没有这一行
    assert called == []
```

扫周期改判定的测试（`TaskSpec` 里没有 `task_id` 字段，所以按 `taskname` 断言，别为测试给生产模型加字段）：

```python
@pytest.mark.asyncio
async def test_daily_sweep_defers_to_retry_job(monkeypatch):
    acc_id = _seed_account()
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    waiting = _persist(taskname="等放出")  # driver=daily → 每日扫驱动
    pending = _persist(taskname="等重试", retry_attempts=1, next_retry_at=datetime.now() + timedelta(minutes=5))
    try:
        summary = await ts.run_tasks(trigger="scheduled")
        assert ran == ["等放出"]  # "等重试"必须让位给它的到点作业
        assert summary["skipped"] == 1 and summary["driven"] == 1
        assert _reload(pending).retry_attempts == 1  # 让位不等于吃预算
    finally:
        _delete(waiting, pending)
        _drop_account(acc_id)
```


- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_once_retry_budget.py -q`
Expected: FAIL —— `AttributeError: 'TaskScheduler' object has no attribute 'reschedule_retry_at'` / `main has no attribute _run_retry_task`。

- [ ] **Step 3: 一次性到点作业**

`backend/core/scheduler.py` 顶部 `from apscheduler.triggers.date import DateTrigger`，类内 `reschedule_task` 之后：

```python
    def reschedule_retry_at(self, task_id: int, when, func) -> str:
        """排一次"到点就跑"的重试作业；到点执行后由结局判定决定是否再排。"""
        job_id = f"xiao_pan_retry_{task_id}"
        self.scheduler.add_job(
            func, trigger=DateTrigger(run_date=when), id=job_id, replace_existing=True,
            max_instances=1, coalesce=True,
        )
        return job_id

    def unschedule_retry(self, task_id: int) -> None:
        job_id = f"xiao_pan_retry_{task_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
```

- [ ] **Step 4: main 接线**

`backend/main.py` 加 `_run_retry_task`（放在 `_run_one_task` 之后），并在 `apply_task_schedule` 里分流：

```python
async def _run_retry_task(task_id: int) -> None:
    """一次性任务的到点重试：先清掉这一格，再走同一套运行与判定。

    必须先清：once_next_driver 见 next_retry_at 非空会答"等重试作业"，
    不清就等于这轮运行自己把自己跳过。
    """
    from .database import session_scope
    from .models import Task
    from .services import task_service

    with session_scope() as s:
        row = s.get(Task, task_id)
        if row is None:
            return
        row.next_retry_at = None
        s.add(row)
    try:
        await task_service.run_tasks(task_ids=[task_id], trigger="scheduled")
    except Exception as exc:  # noqa: BLE001
        hub.make_logger("scheduled")("error", f"任务 {task_id} 重试运行异常：{exc}")
```

```python
def apply_task_schedule(task) -> None:
    from functools import partial

    from .database import session_scope
    from .models import Task, run_mode_of
    from .services.task_service import once_next_driver

    with session_scope() as s:
        row = s.get(Task, task.id)
    if row is None:
        scheduler.unschedule_task(task.id)
        scheduler.unschedule_retry(task.id)
        return
    scheduler.unschedule_task(task.id)  # 形态可能从 follow 改成 once，旧周期作业必须先撤
    driver = once_next_driver(row)
    if row.disabled or run_mode_of(row) == "manual" or driver == "halted":
        scheduler.unschedule_retry(task.id)
        return
    if run_mode_of(row) == "once":
        # 一次性：只有排了到点时间才注册作业；否则交给每日扫"等放出"
        if driver == "retry" and row.next_retry_at is not None:
            scheduler.reschedule_retry_at(row.id, row.next_retry_at, partial(_run_retry_task, row.id))
        else:
            scheduler.unschedule_retry(task.id)
        return
    scheduler.unschedule_retry(task.id)  # 落到 follow 就是周期追更：遗留的到点重试作业必须先撤，否则改形态后仍会被提前跑一次
    scheduler.reschedule_task(task.id, getattr(row, "schedule", "") or "", partial(_run_one_task, task.id))
```

`reschedule_all_tasks()` 末尾无需新增循环——它对每行调 `apply_task_schedule`，重试作业自然按库里的 `next_retry_at` 重建（Step 1 的测试就是钉这件事）。

- [ ] **Step 5: 扫周期改问同一函数**

`backend/services/task_service.py` 的 `_run_tasks_inner`：把现在第 165-171 行这段形态判定（含它上面的中文注释）

```python
        # 仅手动 / 一次性：任何自动触发（全局 crontab 与任务级作业）都不驱动，只能手动点。
        if trigger == "scheduled":
            mode = run_mode_of(task)
            if mode != "follow":
                summary["skipped"] += 1
                tlog("info", f"《{task.taskname}》执行方式为 {mode}，不由定时器驱动")
                continue
```

换成：

```python
        # 仅手动 / 一次性：自动触发能不能驱动这一行，只问 scheduled_should_run（判定的唯一出处）。
        if trigger == "scheduled":
            should, why = scheduled_should_run(task)
            if not should:
                summary["skipped"] += 1
                tlog("info", f"《{task.taskname}》本次不由定时器驱动：{why}")
                continue
```

局部变量 `mode` 随之消失 —— 下面第 177 行之后的分支不读它，若 ruff 报未使用就把那行 `mode = run_mode_of(task)` 一起删掉，不要留成死码。

它下面那条 `has_valid_schedule` 分支：计划初稿说"保持原样"，**执行期作废**。评审发现 `once` + 有效独立 `schedule` 的行会先被 `scheduled_should_run` 判成 `daily`、再被这条分支以「已配置独立调度」跳过，而 `apply_task_schedule` 从不给 once 行注册任务级作业 —— 于是没有任何驱动方，违反 spec 4.3 的「once 且无到点时间 → 参与每日扫」。初稿的正交性论证只覆盖了 `follow` 方向。裁决：这条分支只对 `follow` 行生效（判据用 `run_mode_of(task) == "follow"`），中文文案与 `skipped` 计数口径不变，分支上方那段把 once 也一起描述进去的注释要改成实情。

同理，`apply_task_schedule` 的 follow 落点也要 `scheduler.unschedule_retry(task.id)`：把 once 改成 follow 后，遗留的到点作业仍会提前跑一次并顺手清掉 `next_retry_at`。两处各配一条测试，见 Task 2 Step 1。

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_once_retry_budget.py -q`
Expected: 全绿。旧文案「执行方式为 X，不由定时器驱动」没有任何测试钉过（`grep -rn "不由定时器驱动" backend/tests` 为空），改措辞不会打破基线；如果 Step 1 那条"每日扫让位"断言不稳，按实现事实把断言写得更具体（例如断 `ran == ["等放出"]` 与 `summary["skipped"] == 1`）——**不要**把断言删空。

- [ ] **Step 7: 全量 + ruff + 提交**

```bash
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m ruff check backend
git add backend/core/scheduler.py backend/main.py backend/services/task_service.py backend/tests/test_once_retry_budget.py
git commit -m "feat(tasks): 一次性任务用 DateTrigger 到点重试，重启后按库内时间重建作业"
```

---

## Task 3: 结局写库与手动重新开启

**Files:**
- Modify: `backend/services/task_service.py`（`_settle_once` 扩展、`reset_once_budget`）
- Modify: `backend/api/routes_tasks.py`（`run_one` 前置归零）
- Test: `backend/tests/test_once_retry_budget.py`（追加）

**Interfaces:**
- Consumes: `once_next_driver`、`ONCE_RETRY_*`、`_once_verdict`
- Produces:
  - `task_service.reset_once_budget(task_id: int) -> None`
  - `_settle_once(task, result, counts, tlog)` 的新写库契约：`retry_attempts` / `next_retry_at` / `disabled`
  - 依赖 Task 2 的调度：写库后调用 `apply_task_schedule` 让作业与数据同步

- [ ] **Step 1: 写失败测试**

追加（辅助函数与断言都用 Task 2 已经建好的 `_persist` / `_reload` / `_delete` / `client` / `_seed_account` / `_drop_account`，别再重复定义一份；`_download_for_task` 的计数是按 `✅`/`❌` 前缀行数算的（task_service.py:294-298），所以下载桩直接返回行文本列表即可）：

```python
def _run_once(monkeypatch, status: str, *, download_lines=None, auto_download=False, attempts=0):
    """跑一次带指定结局的运行，返回 (id, 落库后的行)。"""
    acc_id = _seed_account()
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    files = [SavedFile(share_name="1.mp4", final_name="1.mp4", new_fid="f1", dest_path="/x/1.mp4")]

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return TaskRunResult(status=status, files=files if status == "updated" else [], message="转存炸了")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    if download_lines is not None:
        from backend.services import download_service

        async def fake_download(*a, **k):
            return download_lines

        monkeypatch.setattr(download_service, "download_task_files", fake_download)

    tid = _persist(account_id=acc_id, auto_download=auto_download, retry_attempts=attempts)
    asyncio.run(ts.run_tasks(task_ids=[tid], trigger="manual"))
    return tid, _reload(tid)


def test_no_release_does_not_consume_budget(monkeypatch):
    _tid, row = _run_once(monkeypatch, "no_changes")
    assert row.disabled is False and row.retry_attempts == 0 and row.next_retry_at is None


def test_real_failure_consumes_one_and_schedules_five_minutes(monkeypatch):
    _tid, row = _run_once(monkeypatch, "failed")
    assert row.retry_attempts == 1 and row.disabled is False
    assert row.next_retry_at is not None
    delta = (row.next_retry_at - datetime.now()).total_seconds()
    assert 4 * 60 - 20 <= delta <= 5 * 60 + 20


def test_third_failure_exhausts_the_budget(monkeypatch):
    _tid, row = _run_once(monkeypatch, "failed", attempts=2)
    assert row.retry_attempts == 3 and row.next_retry_at is None and row.disabled is False


def test_download_failure_also_counts_as_real_failure(monkeypatch):
    _tid, row = _run_once(
        monkeypatch, "updated", auto_download=True, download_lines=["✅ 1.mp4", "❌ 2.mp4: HTTP 500"]
    )
    assert row.retry_attempts == 1 and row.disabled is False and row.next_retry_at is not None


def test_success_settles_and_clears_everything(monkeypatch):
    _tid, row = _run_once(monkeypatch, "updated", auto_download=True, download_lines=["✅ 1.mp4"], attempts=2)
    assert row.disabled is True and row.retry_attempts == 0 and row.next_retry_at is None


def test_manual_run_resets_the_budget(client, monkeypatch):
    """点行内「▶ 运行」= 重新给三次预算，并撤掉已排上的那一格，这是"手动再次开启"的唯一入口。"""
    acc_id = _seed_account()
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)
    tid = _persist(retry_attempts=1, next_retry_at=datetime.now() + timedelta(minutes=5), account_id=acc_id)
    main.apply_task_schedule(_reload(tid))  # 先让到点作业真实存在
    assert scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}") is not None

    async def fake_run(task_ids=None, trigger="manual"):
        return {"run_id": "x", "total": 1, "updated": 0, "skipped": 0, "failed": 0,
                "disabled_skipped": 0, "driven": 1, "notify_lines": 0}

    monkeypatch.setattr(ts, "run_tasks", fake_run)
    try:
        resp = client.post(f"/api/tasks/{tid}/run")  # TestClient 会把 SSE 响应体读完，后台任务自然跑完
        assert resp.status_code == 200
        row = _reload(tid)
        assert row.retry_attempts == 0 and row.next_retry_at is None
        # 那一格必须一起撤掉，否则用户看到的是"点一下运行，五分钟后又莫名跑了一次"
        assert scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}") is None
    finally:
        _delete(tid)
        _drop_account(acc_id)


def test_settle_once_write_failure_is_swallowed(monkeypatch):
    """收口自己吞异常：抛回运行循环就会砍掉整批（旁路记账铁律，口径同 50028e1/0ed6400）。

    直接打 `_settle_once` 而不是整条 `run_tasks`：`test_task_run_mode` 里现成的
    `_scope_that_fails_on_disable_writes` 只在 `disabled` 被置真时抛，模拟不了
    "写 retry_attempts / next_retry_at 时锁库"，所以这里换成对所有 Task 写入都抛的桩，
    把 `last_run_at` 那次写库排除在爆炸半径之外。
    """
    from contextlib import contextmanager

    from backend.services.task_service import DownloadCounts

    @contextmanager
    def failing_scope():
        with session_scope() as session:
            real_add = session.add

            def add(obj):
                if isinstance(obj, Task):
                    raise RuntimeError("database is locked")
                return real_add(obj)

            session.add = add
            yield session

    tid = _persist()
    monkeypatch.setattr(ts, "session_scope", failing_scope)
    try:
        ts._settle_once(_reload(tid), TaskRunResult(status="failed", message="转存炸了"), DownloadCounts(),
                        lambda level, msg: None)  # 不许抛
        assert _reload(tid).retry_attempts == 0  # 写不进去就是没写，不能假装加了
    finally:
        monkeypatch.undo()
        _delete(tid)
```

`test_settle_once_write_failure_is_swallowed` 之外的四条批次级不崩断言（通知汇总、后续任务照跑）在 `test_task_run_mode.py` 已有对应用例，这里不重复。

- [ ] **Step 2: 跑测试确认失败**

Run: `.venv/bin/python -m pytest backend/tests/test_once_retry_budget.py -q -k "budget or release or failure or success or manual"`
Expected: FAIL —— 现在 `_settle_once` 只写 `disabled`，`retry_attempts` 保持 0、`next_retry_at` 一直 `None`。

- [ ] **Step 3: 结局写库**

`backend/services/task_service.py` 把 `_settle_once` 换成（保持既有的"旁路写库不许抛异常"包裹与复查逻辑）：

```python
def _settle_once(task, result, counts: DownloadCounts, tlog) -> None:
    """一次性任务的三种结局：拿到手 → 停用；还没放出 → 不占预算；真失败 → 吃一次预算。

    这是旁路记账：落库失败只记一条 warn，绝不抛回运行循环——否则本批余下任务不跑，
    已生成的 notify_lines 也一起丢掉，而前端只看到干净的 done。口径同下载账本。
    """
    if run_mode_of(task) != "once":
        return
    done, reason = _once_verdict(task, result, counts)
    now = datetime.now()
    try:
        with session_scope() as session:
            row = session.get(Task, task.id)
            if row is None or row.disabled:
                return  # 运行中被删/已被别处停用：不动作
            mode_now = run_mode_of(row)
            if mode_now != "once":
                return  # 期间用户改回「定时追更」：不再由这里停用或计数
            if done:
                row.disabled = True
                row.retry_attempts = 0
                row.next_retry_at = None
                action = "info"
                msg = f"《{row.taskname}》一次性任务已完成并自动停用（{reason}）"
            elif result.status == "no_changes":
                row.next_retry_at = None  # 还没放出：不占预算，等下一次每日扫再来看
                action = "info"
                msg = f"《{row.taskname}》本次没有新增资源（还没放出或早已转存过），不占重试预算，等下次定时检查"
            else:
                row.retry_attempts = int(row.retry_attempts or 0) + 1
                if row.retry_attempts >= ONCE_RETRY_LIMIT:
                    row.next_retry_at = None
                    action = "warn"
                    msg = (
                        f"《{row.taskname}》三次重试仍未成功（{reason}），已停止自动重试；"
                        "点该行「▶ 运行」可重新开始"
                    )
                else:
                    row.next_retry_at = now + timedelta(minutes=ONCE_RETRY_DELAY_MINUTES)
                    action = "warn"
                    msg = (
                        f"《{row.taskname}》本次未成功（{reason}），"
                        f"{ONCE_RETRY_DELAY_MINUTES} 分钟后重试（{row.retry_attempts}/{ONCE_RETRY_LIMIT}）"
                    )
            session.add(row)
    except Exception as exc:  # noqa: BLE001 旁路记账
        tlog("warn", f"《{task.taskname}》一次性收口失败（不影响运行）：{exc}")
        return
    tlog(action, msg)
    _resync_schedule(task.id)  # 写完到点时间/停用，必须让作业与数据对齐，否则重启前这一格没人跑


def _resync_schedule(task_id: int) -> None:
    """写完 next_retry_at / disabled 后让调度器与数据对齐；导不到就只记日志，不影响主流程。"""
    try:
        from ..main import apply_task_schedule

        with session_scope() as session:
            row = session.get(Task, task_id)
        if row is not None:
            apply_task_schedule(row)
    except Exception as exc:  # noqa: BLE001
        hub.publish("warn", f"任务 {task_id} 调度同步失败：{exc}")
```

`from datetime import datetime, timedelta` 若该文件只导了 `datetime`，补 `timedelta`（第 8 行现在确实只有 `datetime`）。

**已知交互（不要"顺手修"，也不要在本任务里加状态）：** 转存成功但下载有失败会吃一次预算并排 5 分钟后的重试；那一轮重跑转存只会得到 `no_changes`，于是链条自然终结、行回到每日扫等放出。这是刻意接受的：5 分钟后重跑转存治不了本地下载失败，下载失败的正解仍是下载页逐条重下（`_once_verdict` 的 reason 已经把用户指到那里）。no_changes 那条日志的措辞因此写成"还没放出**或早已转存过**"，不假装知道是哪一种。

- [ ] **Step 4: 手动归零**

同文件加：

```python
def reset_once_budget(task_id: int) -> None:
    """行内「▶ 运行」= 重新给三次预算；只对 once 行生效，不偷偷取消停用。

    清 next_retry_at 之后必须把已排上的到点作业也撤掉：否则这一格稍后还会自己跑一次，
    用户看到的就成了"点一下运行，五分钟后又莫名跑了一次"。
    """
    try:
        with session_scope() as session:
            row = session.get(Task, task_id)
            if row is None or run_mode_of(row) != "once":
                return
            had_slot = row.next_retry_at is not None
            row.retry_attempts = 0
            row.next_retry_at = None
            session.add(row)
        if had_slot:
            from ..main import scheduler

            scheduler.unschedule_retry(task_id)
    except Exception as exc:  # noqa: BLE001 旁路写库
        hub.publish("warn", f"任务 {task_id} 重置重试预算失败：{exc}")
```

`backend/api/routes_tasks.py` 的 `run_one`（第 155-158 行）在返回 SSE 之前调用它：

```python
@router.post("/{task_id}/run")
async def run_one(task_id: int) -> StreamingResponse:
    """立即运行单个任务，SSE 流式返回日志。

    对一次性任务，这一按就是「手动再次开启」：先归零预算再跑，否则用尽后永远出不来。
    """
    from ..services.task_service import reset_once_budget

    reset_once_budget(task_id)
    return _sse_stream([task_id], "manual")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `.venv/bin/python -m pytest backend/tests/test_once_retry_budget.py -q`
Expected: 全绿。

- [ ] **Step 6: 全量 + ruff + 提交**

```bash
.venv/bin/python -m pytest backend/tests -q
.venv/bin/python -m ruff check backend
git add backend/services/task_service.py backend/api/routes_tasks.py backend/tests/test_once_retry_budget.py
git commit -m "feat(tasks): 一次性任务三种结局写库，手动运行即重新开启重试预算"
```

---

## Task 4: 界面、文档与真机验收

**Files:**
- Modify: `frontend/src/api/types.ts`、`frontend/src/components/TaskRow.vue`、`frontend/src/components/TaskForm.vue`
- Modify: `README.md`、`docs/superpowers/specs/2026-10-05-task-run-modes-design.md`
- Test: `npm run typecheck` + `npm run build` + 真机验证

**Interfaces:**
- Consumes: `retry_attempts` / `next_retry_at`（Task 1 起在 `TaskOut` 里）
- Produces: 四个互斥徽标与 tooltip；「执行方式」文案更新；上游 spec 的修订指向

- [ ] **Step 1: 类型与徽标**

`frontend/src/api/types.ts` 的 `Task` 加：

```ts
  retry_attempts: number;
  next_retry_at: string | null;
```

`TaskRow.vue` 的 `chips`（第 90-113 行）里，把现有两条 `once` 徽标（第 95-96 行）替换成按 spec 4.6 顺序取一的实现。`chips` 的元素类型要先加 `title?: string`（第 92 行那个内联类型），模板第 139-145 行的 `<span class="chip">` 上加 `:title="c.title"` —— 用原生 title 属性，**不引 `el-tooltip`**（一行文本提示不值得为它把 span 换成组件、也不改现有样式）：

```ts
// 徽标取值互斥、按此顺序第一个命中即用（spec 4.6）：过期排最前，它让其他三种都失去意义。
const ONCE_RETRY_LIMIT_TEXT = 3; // 与后端 ONCE_RETRY_LIMIT 同值；前端拿不到常量，改了后端要同步这里

function enddatePassed(enddate: string): boolean {
  if (!enddate) return false;
  const d = new Date(`${enddate}T23:59:59`);
  return !Number.isNaN(d.getTime()) && d.getTime() < Date.now();
}
```

```ts
  if (t.run_mode === "once") {
    if (enddatePassed(t.enddate)) list.push({ key: "o", text: "已过截止" });
    else if (t.disabled) list.push({ key: "o", text: "已完成", success: true });
    else if (t.next_retry_at)
      list.push({
        key: "o",
        text: `重试中 ${t.retry_attempts}/${ONCE_RETRY_LIMIT_TEXT}`,
        title: `下次重试：${formatTime(t.next_retry_at)}`,
      });
    else if (t.retry_attempts >= ONCE_RETRY_LIMIT_TEXT)
      list.push({ key: "o", text: "重试已用尽", title: "点 ▶ 运行重新开启" });
    else list.push({ key: "o", text: "一次性待执行", primary: true });
  }
```

时间格式化复用 `utils.ts` 已有的 `formatTime`（它已能处理后端 `isoformat()` 出来的无时区串，与 `last_run_at` 的 `relativeTime` 同一套解析口径）；`enddatePassed` 是新写的本地纯函数，`utils.ts` 里没有等价物，别为它改公共模块。第 118 行的 `isOnceDone` 与模板里灰「停用」徽标的互斥逻辑保持不变（一次性 + 停用 = 已完成，不再重复打灰标）。

- [ ] **Step 2: 「执行方式」文案**

`TaskForm.vue` 第 16 行 `RUN_MODE_OPTIONS` 里 `once` 的 `hint` 改成逐字（这个 hint 已经跟着所选形态渲染在下面，不要再加第二条常驻说明，同一个字段两种提示是噪音）：

```
自动重试到拿到为止；资源没放出不算失败、每天再看一次；连续三次真失败则停摆，点「运行」重新开启
```

- [ ] **Step 3: 文档**

`README.md` 的执行方式段落补一句一次性任务的重试口径（预算 3、间隔 5 分钟、用尽后需手动、没放出不占预算）；`docs/superpowers/specs/2026-10-05-task-run-modes-design.md` 在 4.2 与 4.3 各加一行 `> 已被 docs/superpowers/specs/2026-10-05-once-retry-budget-design.md 修订`，不改写原文；同一份新 spec 的 4.1 那行 `once_next_driver(task, now)` 加一行注记：计划阶段砍掉 `now`（无人读取的死参数），实现签名为 `once_next_driver(task)`。

**`README.md` 有另一会话的未提交 DLNA 改动（本计划开工前 `git status` 就是 `M README.md`）：** 只能用 `git add -p README.md` 挑出本次那一个 hunk，**不要** `git add README.md`；若交互分块在当前环境不可用，就把 README 这句改写到 `docs/` 里的那份 spec 段落中并在此步注明"README 待 DLNA 会话提交后再补"，不要替别人的在制品提交。Step 5 的 `git add` 列表里因此默认不含 `README.md`。

- [ ] **Step 4: 检查与真机验收**

```bash
cd frontend && npm run typecheck && npm run build
```

真机（**临时 DATA_DIR 副本**，`crontab` 设远期、所有任务 `disabled=1`、`sign_enabled`/`notify_enabled` 为 false，绝不触发真实转存；端口只用 8000+，**不 kill / 不复用 8432、8433、5173、6800**；`data/xiao_pan.db` 前后 md5 必须一致）逐条取证：

1. 新建 `once` 任务 → 徽标「一次性待执行」。
2. 库里把该行改成 `retry_attempts=1, next_retry_at=+5min` → 刷新后徽标「重试中 1/3」，tooltip 显示本地时间。
3. 改成 `retry_attempts=3, next_retry_at=None` → 「重试已用尽」，tooltip 指向「▶ 运行」。
4. 改成 `enddate` 过去 → 「已过截止」压过其他三种。
5. 改成 `disabled=1, retry_attempts=0` → 「已完成」（不是灰「停用」）。
6. 点「▶ 运行」（可只验证请求与归零效果，不必真跑成功）→ 库里 `retry_attempts` 归零。
7. 后端日志四条中文文案（拿 `GET /api/logs` 或 SSE 抓）逐字对一遍，四条的固定前缀分别是：
   - `《x》一次性任务已完成并自动停用（`
   - `《x》本次没有新增资源（还没放出或早已转存过），不占重试预算，等下次定时检查`
   - `《x》本次未成功（` …… `5 分钟后重试（1/3）`
   - `《x》三次重试仍未成功（` …… `已停止自动重试；点该行「▶ 运行」可重新开始`

不能验证的（例如需要真实放出资源、真实夸克接口）明确写 `NOT VERIFIED + 原因`。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/api/types.ts frontend/src/components/TaskRow.vue frontend/src/components/TaskForm.vue \
  docs/superpowers/specs/2026-10-05-task-run-modes-design.md docs/superpowers/specs/2026-10-05-once-retry-budget-design.md
# README.md 只有在 `git diff README.md` 里没有别人未提交的 DLNA 改动时才一起 add，否则用 git add -p 挑本次 hunk
git commit -m "feat(ui): 一次性任务显示重试中与已用尽徽标，文档同步重试口径"
```

---

## 验收对照（spec → task）

| Spec 条款 | 实现任务 |
| --- | --- |
| 4.1 两列 + 常量 | Task 1 |
| 4.2 三种结局与写库、停摆不写 disabled、中文日志 | Task 1（判定）+ Task 3（写库） |
| 4.3 驱动规则修订 + 单一判定 + DateTrigger + 重启恢复 | Task 1（函数）+ Task 2（接线） |
| 4.4 手动「▶ 运行」= 重新开启 | Task 3 |
| 4.5 与 `enddate` 的关系 | Task 1（`enddate_passed`）+ Task 4（徽标） |
| 4.6 徽标四态与 tooltip、表单文案 | Task 4 |
| 5 测试（含老库升级不空转、旁路写库不砍批次） | Task 1-3 各 Step + Task 4 真机 |
| 2 非目标（不做轻量探测、不加第四形态、不做退避与可配置） | 全程不得出现对应代码 |
