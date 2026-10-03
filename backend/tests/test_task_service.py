"""编排层测试：monkeypatch 路由与假驱动，验证 run_tasks 状态落库与通知聚合。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlmodel import delete, select

from backend.database import session_scope
from backend.drivers.base import CloudDrive, FsItem, SaveResult, ShareBanned, ShareRef
from backend.models import Account, Task
from backend.services import task_service


class OkDriver(CloudDrive):
    key = "fake"
    name = "假盘"
    share_domains = ["fake.example"]
    supported = True
    capability = {"rename"}

    def parse_share(self, url: str) -> ShareRef:
        return ShareRef(url=url, extra={"path_fids": {"": "0"}})

    async def list_share(self, ref, path=""):
        return [FsItem(fid="1", name="new.mp4", token="t1", mtime=1)]

    async def list_dir(self, path):
        return []

    async def ensure_dir(self, path):
        return "fid"

    async def save(self, items, dest_path, ref=None):
        return SaveResult(ok=True, saved=[FsItem(fid="9", name=i.name) for i in items])

    async def close(self):
        pass


class BannedDriver(OkDriver):
    async def list_share(self, ref, path=""):
        raise ShareBanned("分享已被取消")


@pytest.fixture(autouse=True)
def clean_db():
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(Account))
    yield
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(Account))


def _seed(driver_key="fake"):
    with session_scope() as s:
        acc = Account(driver_key=driver_key, cookie="x", enabled=True, can_save=True, name="主号")
        s.add(acc)
        s.commit()
        s.refresh(acc)
        t = Task(taskname="剧集", shareurl="https://fake.example/s/1", savepath="/剧", account_id=acc.id)
        s.add(t)
        s.commit()
        s.refresh(t)
        return acc, t


@pytest.mark.asyncio
async def test_run_updates_and_bans(monkeypatch):
    acc, task = _seed()
    monkeypatch.setattr(task_service, "route_driver", lambda url: OkDriver)
    pushed = []

    async def fake_push(title, content, push_config, settings, log):
        pushed.append((title, content))

    monkeypatch.setattr(task_service, "_push", fake_push)

    summary = await task_service.run_tasks(trigger="manual")
    assert summary["updated"] == 1
    with session_scope() as s:
        row = s.get(Task, task.id)
        assert row.last_run_at is not None and row.shareurl_ban == ""
    assert pushed and "添加追更" in pushed[0][1] and "new.mp4" in pushed[0][1]

    # 换失效驱动再跑：落库 shareurl_ban，通知 ❌
    monkeypatch.setattr(task_service, "route_driver", lambda url: BannedDriver)
    pushed.clear()
    summary2 = await task_service.run_tasks(trigger="manual")
    assert summary2["failed"] == 1
    with session_scope() as s:
        row = s.get(Task, task.id)
        assert row.shareurl_ban == "分享已被取消"
    assert pushed and "❌" in pushed[0][1]

    # 已失效任务：再次运行被跳过（不再调用驱动）
    def boom(url):
        raise AssertionError("不应再路由已封禁任务")

    monkeypatch.setattr(task_service, "route_driver", boom)
    summary3 = await task_service.run_tasks(trigger="manual")
    assert summary3["skipped"] == 1


@pytest.mark.asyncio
async def test_no_account_notifies(monkeypatch):
    _, task = _seed()
    with session_scope() as s:
        for a in s.exec(select(Account)).all():
            a.enabled = False
            s.add(a)
    monkeypatch.setattr(task_service, "route_driver", lambda url: OkDriver)

    async def fake_push(title, content, push_config, settings, log):
        pass

    monkeypatch.setattr(task_service, "_push", fake_push)
    summary = await task_service.run_tasks(trigger="manual")
    assert summary["failed"] == 1


def _mktask(tid):
    """内存 Task（不落库），让流程真正走到转存段。"""
    return Task(
        id=tid,
        taskname=f"t{tid}",
        shareurl="https://fake.example/s",
        savepath="/s",
        auto_download=False,
        disabled=False,
    )


@pytest.mark.asyncio
async def test_transfer_phase_serialized_across_calls(monkeypatch):
    """并发两个 run_tasks 时，转存段（run_update_task）从不重叠——全局锁串行化。"""
    active = {"n": 0, "max": 0, "calls": 0}

    class FakeResult:
        status = "no_changes"
        message = ""
        files = []

        def render(self):
            return ""

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        # 记录转存段的重叠进入数：进入 +1，await 让出事件循环，另一路并发进入则 +2
        active["calls"] += 1
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        await asyncio.sleep(0.05)
        active["n"] -= 1
        return FakeResult()

    def fake_pick_account(account_id, driver_key):
        return SimpleNamespace(id=1, cookie="ck", sort_order=0, can_save=True)

    # route_driver 必须返回 supported 驱动、_pick_account 返回账号，流程才会进入 run_update_task
    monkeypatch.setattr(task_service, "run_update_task", fake_run_update)
    monkeypatch.setattr(task_service, "route_driver", lambda url: OkDriver)
    monkeypatch.setattr(task_service, "_pick_account", fake_pick_account)
    monkeypatch.setattr(task_service, "load_tasks", lambda ids=None: [_mktask(1), _mktask(2)])

    await asyncio.gather(
        task_service.run_tasks([1], "scheduled"),
        task_service.run_tasks([2], "scheduled"),
    )

    # 非空转：两次 run 各两任务都真正进入转存段（4 次），否则锁根本没被测到
    assert active["calls"] == 4, "转存段应被实际调用，测试才有意义"
    # 转存段从不重叠：无锁时并发 gather 会让 max==2
    assert active["max"] <= 1
