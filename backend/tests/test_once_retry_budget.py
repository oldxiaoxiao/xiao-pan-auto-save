"""一次性任务的重试预算：驱动判定、预算消耗、停摆与重启恢复。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend import main
from backend.core.engine import TaskRunResult
from backend.core.scheduler import enddate_passed
from backend.database import session_scope
from backend.main import app, scheduler
from backend.models import Account, Task
from backend.services import task_service as ts
from backend.services.task_service import (
    ONCE_RETRY_DELAY_MINUTES,
    ONCE_RETRY_LIMIT,
    once_next_driver,
    scheduled_should_run,
)
from backend.tests.test_task_run_mode import DownloadOkDriver, _drop_account, _seed_account


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
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import StaticPool
    from sqlmodel import select

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
