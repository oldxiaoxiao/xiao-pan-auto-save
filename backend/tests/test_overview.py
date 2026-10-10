"""FR-09 总览：定级、聚合与降级。

总览不新增判定逻辑，只把 FR-01~08 已算好的结论聚到一处——所以这里测的是"聚得对不对"，
以及任何一块读不出来时整页不能崩。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlmodel import delete

from backend.database import session_scope
from backend.models import Account, DownloadRecord, Task, TaskRun
from backend.services import overview_service


@pytest.fixture(autouse=True)
def clean_db():
    with session_scope() as s:
        s.exec(delete(TaskRun))
        s.exec(delete(DownloadRecord))
        s.exec(delete(Task))
        s.exec(delete(Account))
    yield
    with session_scope() as s:
        s.exec(delete(TaskRun))
        s.exec(delete(DownloadRecord))
        s.exec(delete(Task))
        s.exec(delete(Account))


def _task(name="剧集"):
    with session_scope() as s:
        t = Task(taskname=name, shareurl="https://fake.example/s/1", savepath="/剧")
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


def _account(name="主号", **kw):
    with session_scope() as s:
        a = Account(driver_key="fake", cookie="ck", enabled=True, name=name, **kw)
        s.add(a)
        s.commit()
        s.refresh(a)
        return a.id


def _download(status: str, name="f.mp4", dest="/tmp/f.mp4", size=1000):
    with session_scope() as s:
        r = DownloadRecord(
            source="builtin", ref_id=f"r-{status}-{name}", filename=name, dest_path=dest,
            size_total=size, status=status, finished_at=datetime.now(),
        )
        s.add(r)
        s.commit()


# ------------------------------------------------------------ 定级


def test_empty_system_is_ok(tmp_path):
    data = overview_service.build(tmp_path)
    assert data["level"] == "ok"
    assert data["blocking"] == []
    assert data["counts"]["tasks"] == 0
    assert data["last_run"] is None


def test_expired_account_is_critical(tmp_path):
    """账号失效 = 需要用户动手，属于最高级。"""
    aid = _account()
    _task()
    with session_scope() as s:
        row = s.get(Account, aid)
        row.check_ok = False
        row.check_message = "Cookie 已失效"
        row.last_check_at = datetime.now()
        s.add(row)

    data = overview_service.build(tmp_path)
    assert data["level"] == "critical"
    assert any("需更新 Cookie" in b for b in data["blocking"])
    assert data["bad_accounts"][0]["message"] == "Cookie 已失效"


def test_failing_task_is_attention_not_critical(tmp_path):
    """连续失败只是"需要留意"：它不阻塞其它任务，不该和"账号失效"同级。"""
    tid = _task()
    with session_scope() as s:
        for _ in range(3):
            s.add(TaskRun(task_id=tid, status="failed", message="转存失败：网络异常"))
        s.commit()

    data = overview_service.build(tmp_path)
    assert data["level"] == "attention"
    assert data["issues_total"] == 1
    assert data["issues"][0]["fail_streak"] == 3
    assert "网络异常" in data["issues"][0]["reason"]


def test_interrupted_download_is_critical(tmp_path):
    """中断的下载不算"失败"——它能接着下，得单独提示。"""
    _download("interrupted")
    data = overview_service.build(tmp_path)
    assert data["level"] == "critical"
    assert any("可继续" in b for b in data["blocking"])
    assert data["downloads"]["interrupted"] == 1


def test_today_counters_are_separate(tmp_path):
    """今日完成/失败与累计不能混：累计失败很多不代表今天出了问题。"""
    _download("done")
    _download("failed")
    data = overview_service.build(tmp_path)
    assert data["downloads"]["done_today"] == 1
    assert data["downloads"]["failed_today"] == 1
    assert data["downloads"]["in_flight"] == 0


# ------------------------------------------------------------ 降级


def test_unreadable_disk_does_not_break_overview(tmp_path):
    """磁盘读不出来要如实说"不知道"，不能编数字，更不能让整页 500。"""
    missing = tmp_path / "no" / "such" / "dir"
    data = overview_service.build(missing)
    assert data["level"] in ("ok", "attention", "critical")
    assert data["disk"]["known"] is False
    assert "无法读取" in data["disk"].get("reason", "")


def test_last_run_comes_from_ledger(tmp_path):
    """最近一次运行必须来自账本，不拿任务配置猜。"""
    tid = _task("有运行记录的剧")
    with session_scope() as s:
        s.add(TaskRun(task_id=tid, status="updated", message="新增 2 项"))
        s.commit()

    data = overview_service.build(tmp_path)
    assert data["last_run"]["taskname"] == "有运行记录的剧"
    assert data["last_run"]["status"] == "updated"
    assert data["last_run"]["message"] == "新增 2 项"
