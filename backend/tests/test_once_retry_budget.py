"""一次性任务的重试预算：驱动判定、预算消耗、停摆与重启恢复。"""

from __future__ import annotations

from datetime import datetime, timedelta

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
