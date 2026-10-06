"""一次性任务的重试预算：驱动判定、预算消耗、停摆与重启恢复。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from backend import main
from backend.api import routes_tasks
from backend.core.engine import SavedFile, TaskRunResult
from backend.core.logstream import hub
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
from backend.tests.test_task_run_mode import DownloadOkDriver, _drop_account, _seed_account, _spy_logs


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


def test_switching_once_to_follow_withdraws_the_retry_job():
    """once 行挂着到点重试作业后被用户改成 follow：遗留作业必须一并撤销。

    否则它到点仍会触发 _run_retry_task，把刚变成 follow 的行提前跑一次，
    并顺手把 next_retry_at 清掉。
    """
    when = datetime.now() + timedelta(minutes=5)
    tid = _persist(taskname="改回追更", retry_attempts=1, next_retry_at=when, schedule="interval:30")
    try:
        main.apply_task_schedule(_reload(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}") is not None
        with session_scope() as s:
            s.get(Task, tid).run_mode = "follow"
        main.apply_task_schedule(_reload(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}") is None
        assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is not None  # follow 走自己的周期作业
    finally:
        _delete(tid)


@pytest.mark.asyncio
async def test_daily_sweep_drives_once_row_holding_a_valid_schedule(monkeypatch):
    """once + 有效独立 schedule + 无到点时间：必须由每日扫驱动。

    apply_task_schedule 从不给 once 行注册任务级周期作业，has_valid_schedule 跳过分支
    若不收紧到 follow，这行会被"已配置独立调度"拦下——没有任何驱动方，违反 spec 4.3。
    """
    acc_id = _seed_account()
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    stale = _persist(taskname="带调度的一次性", schedule="interval:30")  # driver=daily，无到点时间
    try:
        summary = await ts.run_tasks(trigger="scheduled")
        assert ran == ["带调度的一次性"]  # 旧 schedule 字符串不该饿死它
        assert summary["skipped"] == 0 and summary["driven"] == 1
    finally:
        _delete(stale)
        _drop_account(acc_id)


# ---------- 结局写库：三种结局 + 手动重新开启（Task 3） ----------


def test_expired_once_row_says_past_deadline_not_completed():
    """过期停摆的行不能报「已完成或已停用」——用户会以为资源到手，其实是过了截止日期。"""
    ok, why = scheduled_should_run(_t(enddate="2020-01-01"))
    assert not ok and "截止日期" in why
    # 判序与 once_next_driver 一致：disabled 优先于过期（完成后再过期的行，原因仍该是完成/停用）
    ok, why = scheduled_should_run(_t(disabled=True, enddate="2020-01-01"))
    assert not ok and why == "已完成或已停用"


def _run_once(monkeypatch, status: str, *, download_lines=None, auto_download=False, attempts=0, enddate=""):
    """跑一次带指定结局的运行，返回 (id, 落库后的行)。

    收尾必须走 `_delete`：真失败会经 `_resync_schedule` 注册 5 分钟后的 `xiao_pan_retry_{id}`
    到点作业，只删行不撤作业等于把它留给别人（`_delete` docstring 立的标准）。
    """
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

    tid = _persist(account_id=acc_id, auto_download=auto_download, retry_attempts=attempts, enddate=enddate)
    try:
        asyncio.run(ts.run_tasks(task_ids=[tid], trigger="manual"))
        return tid, _reload(tid)
    finally:
        _delete(tid)


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
    """点行内「▶ 运行」= 重新给三次预算，并撤掉已排上的那一格，这是"手动再次开启"的唯一入口。

    桩必须打在 `routes_tasks.run_tasks` 上：路由顶部是绑定式导入
    （`from ..services.task_service import ... run_tasks`），改 `ts.run_tasks` 拦不住它，
    真实引擎会跑完整条链并走「拿到手」分支把行停用 —— 用例照样绿，却一次都没测到归零。
    """
    acc_id = _seed_account()
    tid = _persist(retry_attempts=1, next_retry_at=datetime.now() + timedelta(minutes=5), account_id=acc_id)
    main.apply_task_schedule(_reload(tid))  # 先让到点作业真实存在
    assert scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}") is not None

    calls: list[tuple[list[int] | None, str]] = []

    async def fake_run(task_ids=None, trigger="manual"):
        calls.append((task_ids, trigger))
        hub.publish("done", "桩：这一轮不进引擎")  # SSE 流靠这条收尾，否则响应永远不关
        return {"run_id": "stub", "total": 1, "updated": 0, "skipped": 0, "failed": 0,
                "disabled_skipped": 0, "driven": 0, "notify_lines": 0}

    monkeypatch.setattr(routes_tasks, "run_tasks", fake_run)
    try:
        resp = client.post(f"/api/tasks/{tid}/run")  # TestClient 会把 SSE 响应体读完，后台任务自然跑完
        assert resp.status_code == 200
        assert calls == [([tid], "manual")]  # 桩真的拦住了路由，引擎一次都没进
        row = _reload(tid)
        assert row.retry_attempts == 0 and row.next_retry_at is None
        # 「▶ 运行」是归零预算，不是替用户收口成已完成：停不停用只由结局判定说了算
        assert row.disabled is False
        # 那一格必须一起撤掉，否则用户看到的是"点一下运行，五分钟后又莫名跑了一次"
        assert scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}") is None
    finally:
        _delete(tid)
        _drop_account(acc_id)


def test_reset_once_budget_only_touches_once_rows():
    """直打 `reset_once_budget` 的单元哨兵：归零两列 + 撤掉已排的作业 + 非 once 行一根手指都不碰。

    不经 HTTP，与上面那条路由用例各钉各的：删掉实现里「归零」「撤格」「只对 once 生效」
    任何一件，这里都有一条断言变红。
    """
    once_tid = _persist(taskname="一次性归零", retry_attempts=2, next_retry_at=datetime.now() + timedelta(minutes=5))
    follow_tid = _persist(taskname="追更不该动", run_mode="follow", retry_attempts=2,
                          next_retry_at=datetime.now() + timedelta(minutes=5))
    manual_tid = _persist(taskname="仅手动不该动", run_mode="manual", retry_attempts=2,
                          next_retry_at=datetime.now() + timedelta(minutes=5))
    main.apply_task_schedule(_reload(once_tid))
    try:
        assert scheduler.scheduler.get_job(f"xiao_pan_retry_{once_tid}") is not None
        ts.reset_once_budget(once_tid)
        row = _reload(once_tid)
        assert row.retry_attempts == 0 and row.next_retry_at is None
        assert row.disabled is False  # 归零 ≠ 悄悄把行送进「已完成」终态
        assert scheduler.scheduler.get_job(f"xiao_pan_retry_{once_tid}") is None
        for tid in (follow_tid, manual_tid):
            before = _reload(tid)
            snap = (before.retry_attempts, before.next_retry_at, before.disabled)
            ts.reset_once_budget(tid)
            after = _reload(tid)
            assert (after.retry_attempts, after.next_retry_at, after.disabled) == snap
    finally:
        _delete(once_tid, follow_tid, manual_tid)


def test_expired_row_counts_the_failure_but_gets_no_retry_slot(monkeypatch):
    """已过截止日期的行真失败：只 +1 计数、不排格。

    排了也没人跑（`once_next_driver` 见过期就判 halted），库里只会留下一格自相矛盾的
    `next_retry_at` —— 正是本特性在别处刻意堵死的那个矛盾态。日志文案同理，不许说「5 分钟后重试」。
    """
    logs = _spy_logs(monkeypatch)
    tid, row = _run_once(monkeypatch, "failed", enddate="2020-01-01")
    assert row.retry_attempts == 1 and row.disabled is False
    assert row.next_retry_at is None
    mine = [msg for task_id, _, msg in logs if task_id == tid]
    assert not [m for m in mine if "分钟后重试" in m], mine


def test_third_failure_logs_the_handoff_hint(monkeypatch):
    """停摆必须把下一步动作直接告诉用户（design 4.2），并且铁律不写 `disabled`。"""
    logs = _spy_logs(monkeypatch)
    tid, row = _run_once(monkeypatch, "failed", attempts=2)
    mine = [msg for task_id, _, msg in logs if task_id == tid]
    assert any("三次重试仍未成功" in m and "点该行「▶ 运行」可重新开始" in m for m in mine), mine
    assert row.disabled is False


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


# ---------- C1 回归：过期的重试作业不能被静默丢弃 ----------


def test_overdue_retry_slot_registers_job_without_misfire_grace(monkeypatch):
    """重启重建出来的过期作业必须显式关掉宽限期：APScheduler 默认 1s 宽限会把它丢弃。

    丢的链路（评审在仓库 venv 实证过）：停机期间格子到期 → reschedule_all_tasks 注册过期作业
    → 调度器按默认宽限打 "was missed"、不执行并移除 → 库里格子仍在 ⇒ once_next_driver 答
    "retry" ⇒ 每日扫永远让位 ⇒ 这一行永久失去驱动方，界面却还显示「重试中」和一个已过去的 ETA。
    misfire_grace_time=None 意为"迟到多久都照跑"，正是 spec 4.3 重启重建的补跑语义。
    """
    async def fake_run(task_ids=None, trigger="manual"):
        return {"run_id": "x"}

    monkeypatch.setattr(ts, "run_tasks", fake_run)
    # 在注册那一刻拿到作业对象：调度器已启动时，过期格子可能转瞬跑完自我移除，事后 get_job 不稳
    registered: list = []
    real = main.scheduler.reschedule_retry_at

    def spy(task_id, when, func):
        job_id = real(task_id, when, func)
        job = main.scheduler.scheduler.get_job(job_id)
        if job is not None:
            registered.append(job)
        return job_id

    monkeypatch.setattr(main.scheduler, "reschedule_retry_at", spy)
    tid = _persist(retry_attempts=1, next_retry_at=datetime.now() - timedelta(minutes=6))
    try:
        main.reschedule_all_tasks()
        assert len(registered) == 1
        assert registered[0].misfire_grace_time is None
    finally:
        _delete(tid)


@pytest.mark.asyncio
async def test_overdue_retry_job_catches_up_and_drives_the_row(client, monkeypatch):
    """钉死"过期格子不会让这一行永久失去驱动方"：注册即补跑，格子真的被驱动。

    修复前这条红：过期作业被 "was missed" 丢弃、一次都没执行，而每日扫对它永久让位——
    本特性「重启后按库内时间重建」的承诺就此落空。
    """
    ran: list[int] = []

    async def fake_run(task_ids=None, trigger="manual"):
        ran.append(task_ids[0])
        return {"run_id": "x"}

    monkeypatch.setattr(ts, "run_tasks", fake_run)
    tid = _persist(retry_attempts=1, next_retry_at=datetime.now() - timedelta(minutes=6))
    try:
        main.apply_task_schedule(_reload(tid))
        for _ in range(50):  # 过期补跑应立刻发生；5s 只是调度抖动的上限，不是等待阈值
            if ran:
                break
            await asyncio.sleep(0.1)
        assert ran == [tid], "过期重试作业被静默丢弃，这一行已无任何驱动方"
        assert _reload(tid).next_retry_at is None  # 跑前清格：让位逻辑得以闭环的前提
    finally:
        _delete(tid)


@pytest.mark.asyncio
async def test_retry_slot_clear_failure_re_registers_job(monkeypatch):
    """跑前清格的写库撞 "database is locked" 时：吞异常、不裸跑、且必须再排一格。

    抛回调度器 = 这格作业被消费移除而库里格子还在 ⇒ 每日扫永远让位，与 misfire 丢作业
    是同一种孤儿态；不重新注册则同样把行丢成无人驱动。宁可让重试作业继续驱动
    （按 5 分钟节奏到点再清再跑），也不能让它永远让位。
    """
    from contextlib import contextmanager

    import backend.database

    ran: list[int] = []

    async def fake_run(task_ids=None, trigger="manual"):
        ran.append(task_ids[0])
        return {"run_id": "x"}

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

    monkeypatch.setattr(ts, "run_tasks", fake_run)
    monkeypatch.setattr(backend.database, "session_scope", failing_scope)
    tid = _persist(retry_attempts=1, next_retry_at=datetime.now() - timedelta(minutes=1))
    try:
        await main._run_retry_task(tid)  # 不许抛回调度器
        assert ran == []  # 格没清掉就不裸跑（判定会自我跳过），把这一轮让回给下一次到点
        job = scheduler.scheduler.get_job(f"xiao_pan_retry_{tid}")
        assert job is not None, "写失败后这一行仍须有驱动方，不能留下格在、作业已丢的孤儿态"
        assert _naive_run_date(job) > datetime.now()  # 按重试节奏重排，不能拿过期格立刻补跑——锁不放开就是热循环
        assert _reload(tid).retry_attempts == 1  # 写失败就是失败：没清掉不能假装清掉了
    finally:
        monkeypatch.undo()  # _reload/_delete 用的是测试模块顶部的原始绑定，但撤桩要在断言后立刻做
        _delete(tid)


# ---------- I1 回归：runweek 那道门只关得住 follow 行 ----------


@pytest.mark.asyncio
async def test_legacy_runweek_gates_follow_rows_only_not_once_rows(monkeypatch):
    """once 行带着"今天不在其中"的遗留 runweek 仍须被每日扫驱动。

    表单切到 once 只隐藏不清空 runweek（follow→once 可达），而 spec 4.3 说 once 无到点时间
    「参与每日扫（每天看一次，就是等放出）」、界面提示也写着"每天再看一次"。若 runweek 门
    连 once 一起拦，这行会连续最多 6 天没人看，连到点重试作业触发时都被跳过——徽标还挂着
    「重试中」。enddate 那条腿不受影响：once 的过期判定本就由 once_next_driver 承担。
    """
    acc_id = _seed_account()
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    off_day = next(d for d in range(1, 8) if d != datetime.now().isoweekday())  # 今天必然不在这一周内
    once_row = _persist(taskname="带遗留runweek的一次性", runweek=json.dumps([off_day]))
    follow_row = _persist(taskname="按runweek该歇的追更", run_mode="follow", runweek=json.dumps([off_day]))
    try:
        summary = await ts.run_tasks(trigger="scheduled")
        assert ran == ["带遗留runweek的一次性"]  # once 照跑，follow 照旧被 runweek 拦
        assert summary["skipped"] == 1 and summary["driven"] == 1
    finally:
        _delete(once_row, follow_row)
        _drop_account(acc_id)
