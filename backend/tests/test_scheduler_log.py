import json
from datetime import date

from backend.core.logstream import LogHub
from backend.core.scheduler import task_due_today
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
