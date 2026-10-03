"""编排层测试：monkeypatch 路由与假驱动，验证 run_tasks 状态落库与通知聚合。"""

from __future__ import annotations

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
