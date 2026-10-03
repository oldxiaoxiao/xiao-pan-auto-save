import asyncio
import json
from datetime import date

from backend.core.logstream import LogHub
from backend.core.scheduler import TaskScheduler, has_valid_schedule, task_due_today
from backend.models import Task


class FakeTask:
    def __init__(self, runweek="[]", enddate="", disabled=False):
        self.runweek = runweek
        self.enddate = enddate
        self.disabled = disabled

    def runweek_list(self):
        return [int(x) for x in json.loads(self.runweek or "[]")]


def test_runweek_empty_runs_every_day():
    assert task_due_today(FakeTask(), today=date(2026, 10, 5))  # 周一


def test_runweek_matches_isoweekday():
    monday = date(2026, 10, 5)
    assert monday.isoweekday() == 1
    assert task_due_today(FakeTask(runweek="[1,3,5]"), today=monday)
    assert not task_due_today(FakeTask(runweek="[2,4]"), today=monday)


def test_enddate():
    assert task_due_today(FakeTask(enddate="2026-10-05"), today=date(2026, 10, 5))
    assert not task_due_today(FakeTask(enddate="2026-10-04"), today=date(2026, 10, 5))
    assert task_due_today(FakeTask(enddate="坏格式"), today=date(2026, 10, 5))  # 坏格式宽容处理


def test_disabled():
    assert not task_due_today(FakeTask(disabled=True))


def test_model_runweek_list():
    t = Task(taskname="x", shareurl="u", savepath="/s", runweek="[1,7]")
    assert t.runweek_list() == [1, 7]
    t.runweek = "bad"
    assert t.runweek_list() == []


def test_loghub_publish_history_and_queue(tmp_path):
    hub = LogHub(log_dir=tmp_path, keep=10)
    q = hub.subscribe()
    log = hub.make_logger("run1", task_id=3)
    log("info", "hello")
    assert q.qsize() == 1
    entry = q.get_nowait()
    assert entry["message"] == "hello" and entry["run_id"] == "run1" and entry["task_id"] == 3
    assert len(hub.history) == 1
    logs = list(tmp_path.glob("runtime-*.log"))
    assert logs and "hello" in logs[0].read_text(encoding="utf-8")
    hub.unsubscribe(q)
    log("info", "after unsub")
    assert q.empty()


def test_reschedule_task_interval_and_cron():
    # AsyncIOScheduler 需要运行中的事件循环，同步用例里手动跑一个短循环
    async def run():
        s = TaskScheduler()
        s.start()
        try:
            assert s.reschedule_task(1, "interval:5", lambda: None) is not None
            assert s.scheduler.get_job("xiao_pan_task_1") is not None
            assert s.reschedule_task(2, "cron:*/10 * * * *", lambda: None) is not None
            # 非法 cron → 不注册、不崩
            assert s.reschedule_task(3, "cron:not-a-cron", lambda: None) is None
            assert s.scheduler.get_job("xiao_pan_task_3") is None
            # 空 schedule → 撤销单独注册
            s.reschedule_task(1, "", lambda: None)
            assert s.scheduler.get_job("xiao_pan_task_1") is None
            s.unschedule_task(2)
            assert s.scheduler.get_job("xiao_pan_task_2") is None
        finally:
            s.shutdown()

    asyncio.run(run())


def test_reschedule_task_invalid_cron_removes_old_job():
    # FIX 1：从有效 schedule 改成非法后，旧 job 必须被撤销，不能继续按旧调度触发
    async def run():
        s = TaskScheduler()
        s.start()
        try:
            assert s.reschedule_task(9, "interval:5", lambda: None) is not None
            assert s.scheduler.get_job("xiao_pan_task_9") is not None
            # 改成非法 cron → 返回 None 且撤销旧 job
            assert s.reschedule_task(9, "cron:not-a-cron", lambda: None) is None
            assert s.scheduler.get_job("xiao_pan_task_9") is None
        finally:
            s.shutdown()

    asyncio.run(run())


def test_has_valid_schedule():
    # FIX 3 依赖：任务自带有效 schedule 判断，复用 TaskScheduler._parse_trigger
    assert has_valid_schedule("interval:5")
    assert has_valid_schedule("cron:*/10 * * * *")
    assert not has_valid_schedule("")
    assert not has_valid_schedule("cron:not-a-cron")
    assert not has_valid_schedule("unknown:1")


async def test_run_one_task_self_unschedules_when_expired(monkeypatch):
    from datetime import timedelta

    from backend import main
    from backend.database import session_scope

    unscheduled = []
    monkeypatch.setattr(main.scheduler, "unschedule_task", lambda tid: unscheduled.append(tid))
    ran = []

    async def fake_run(task_ids=None, trigger=None):
        ran.append(task_ids)
        return {}

    monkeypatch.setattr("backend.services.task_service.run_tasks", fake_run)
    past = (date.today() - timedelta(days=1)).isoformat()
    with session_scope() as s:
        t = Task(taskname="过期任务", shareurl="https://x/s", savepath="/s", schedule="interval:5", enddate=past)
        s.add(t)
        s.commit()
        s.refresh(t)
        tid = t.id
    try:
        await main._run_one_task(tid)
        assert unscheduled == [tid] and ran == []
    finally:
        with session_scope() as s:
            row = s.get(Task, tid)
            if row:
                s.delete(row)
