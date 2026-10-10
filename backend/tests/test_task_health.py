"""FR-03 任务健康度与失败归因：连续失败、失效链接、账号异常、长期未运行。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete

from backend.database import session_scope
from backend.drivers.base import CloudDrive, FsItem, SaveResult, ShareRef
from backend.models import Account, Task, TaskRun
from backend.services import task_health


class FakeDriver(CloudDrive):
    key = "fake"
    name = "假盘"
    share_domains = ["fake.example"]
    supported = True
    capability = {"rename"}

    def parse_share(self, url: str) -> ShareRef:
        return ShareRef(url=url)

    async def list_share(self, ref, path=""):
        return [FsItem(fid="1", name="a.mp4")]

    async def list_dir(self, path):
        return []

    async def ensure_dir(self, path):
        return "fid"

    async def save(self, items, dest_path, ref=None):
        return SaveResult(ok=True, saved=[])

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def clean_db():
    with session_scope() as s:
        s.exec(delete(TaskRun))
        s.exec(delete(Task))
        s.exec(delete(Account))
    yield
    with session_scope() as s:
        s.exec(delete(TaskRun))
        s.exec(delete(Task))
        s.exec(delete(Account))


def _seed(**kw):
    kw.setdefault("taskname", "剧集")
    kw.setdefault("shareurl", "https://fake.example/s/1")
    kw.setdefault("savepath", "/剧")
    with session_scope() as s:
        t = Task(**kw)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t


def _runs(task_id: int, statuses: list[str], message: str = "转存失败：网络异常") -> None:
    for st in statuses:
        task_health.record_run(task_id, st, message if st in task_health.FAIL_STATUSES else "")


# ------------------------------------------------------------ 连续失败判定


def test_three_consecutive_failures_flag_attention():
    t = _seed()
    _runs(t.id, ["failed", "failed", "failed"])

    h = task_health.compute_health(t)
    assert h["status"] == "attention"
    assert h["kind"] == "failing"
    assert h["fail_streak"] == 3
    assert "网络异常" in h["reason"], "必须给出最近一次失败原因"


def test_success_resets_streak():
    t = _seed()
    _runs(t.id, ["failed", "failed", "updated", "failed"])
    h = task_health.compute_health(t)
    assert h["status"] == "ok", "中间成功一次就该清零"
    assert h["fail_streak"] == 1


def test_no_changes_is_not_failure():
    """没更新≠失败。把 no_changes 算成失败会让所有长期不更新的剧被误判。"""
    t = _seed()
    _runs(t.id, ["no_changes"] * 5)
    assert task_health.compute_health(t)["status"] == "ok"


# ------------------------------------------------------------ 各类异常信号


def test_banned_share_flagged():
    t = _seed(shareurl_ban="分享已被取消")
    h = task_health.compute_health(t)
    assert h["status"] == "attention" and h["kind"] == "banned"
    assert "分享已被取消" in h["reason"]


def test_invalid_account_flagged():
    with session_scope() as s:
        acc = Account(driver_key="fake", cookie="x", enabled=True, name="主号",
                      check_ok=False, check_message="Cookie 已失效，请更新后重试",
                      last_check_at=datetime.now())
        s.add(acc)
        s.commit()
        s.refresh(acc)
        acc_id = acc.id
    t = _seed(account_id=acc_id)
    h = task_health.compute_health(t)
    assert h["status"] == "attention" and h["kind"] == "account"
    assert "主号" in h["reason"]


def test_unchecked_account_not_flagged():
    """没检查过的账号不算异常（与 FR-02 同口径）。"""
    with session_scope() as s:
        acc = Account(driver_key="fake", cookie="x", enabled=True, check_ok=False)
        s.add(acc)
        s.commit()
        s.refresh(acc)
        acc_id = acc.id
    t = _seed(account_id=acc_id)
    assert task_health.compute_health(t)["status"] == "ok"


def test_stale_when_not_run_for_days():
    t = _seed(last_run_at=datetime.now() - timedelta(days=5))
    h = task_health.compute_health(t)
    assert h["status"] == "stale" and h["kind"] == "stale"


def test_never_run_is_ok():
    """新建还没跑过的任务不该被判停摆。"""
    t = _seed()
    assert task_health.compute_health(t)["status"] == "ok"


# ------------------------------------------------------------ 聚合视图


def test_issues_lists_only_unhealthy():
    good = _seed(taskname="正常剧")
    bad = _seed(taskname="失效剧", shareurl_ban="分享已被取消")
    _runs(good.id, ["updated"])

    rows = task_health.issues()
    ids = {r["id"] for r in rows}
    assert bad.id in ids
    assert good.id not in ids
    hit = next(r for r in rows if r["id"] == bad.id)
    assert hit["reason"]


def test_recent_runs_are_readable():
    t = _seed()
    _runs(t.id, ["failed", "updated"])
    runs = task_health.recent_runs(t.id)
    assert len(runs) == 2
    assert runs[0]["status"] == "updated"  # 最新的在前


# ------------------------------------------------------------ 重新验证链接


@pytest.mark.asyncio
async def test_revalidate_clears_ban_when_share_recovered(monkeypatch):
    from backend.api import routes_files, routes_tasks
    from backend.core import router

    t = _seed(shareurl_ban="分享已被取消")
    monkeypatch.setattr(router, "route_driver", lambda url: FakeDriver)
    monkeypatch.setattr(routes_files, "_driver", lambda key: FakeDriver())

    res = await routes_tasks.revalidate(int(t.id or 0))
    assert res["ok"] is True, res
    with session_scope() as s:
        assert s.get(Task, t.id).shareurl_ban == ""


@pytest.mark.asyncio
async def test_revalidate_keeps_ban_when_still_dead(monkeypatch):
    """没恢复就不许乐观清除：否则下次运行还是静默失败。"""
    from backend.api import routes_files, routes_tasks
    from backend.core import router
    from backend.drivers.base import ShareBanned

    class DeadDriver(FakeDriver):
        async def list_share(self, ref, path=""):
            raise ShareBanned("分享已被取消")

    t = _seed(shareurl_ban="分享已被取消")
    monkeypatch.setattr(router, "route_driver", lambda url: DeadDriver)
    monkeypatch.setattr(routes_files, "_driver", lambda key: DeadDriver())

    res = await routes_tasks.revalidate(int(t.id or 0))
    assert res["ok"] is False
    with session_scope() as s:
        assert s.get(Task, t.id).shareurl_ban == "分享已被取消"
