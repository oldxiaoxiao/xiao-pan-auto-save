"""FR-01 运行前资源预检：磁盘空间、网盘配额、限流退避。

共同契约：这三类中断必须在**动作开始前**被拦下并说清原因，
不能等写了一半才失败、更不能静默无提示。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import delete

from backend.database import session_scope
from backend.drivers.base import CloudDrive, FsItem, SaveResult, ShareRef
from backend.models import Account, DriveBackoff, Task
from backend.services import download_history, download_service, resource_guard, task_service
from backend.services.download_service import DownloadItem, DownloadSettings

GB = 1024**3


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


@pytest.fixture(autouse=True)
def clean_db():
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(Account))
        s.exec(delete(DriveBackoff))
    yield
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(Account))
        s.exec(delete(DriveBackoff))


def _seed(capacity_total: int = 0, capacity_used: int = 0):
    with session_scope() as s:
        acc = Account(
            driver_key="fake", cookie="x", enabled=True, can_save=True, name="主号",
            capacity_total=capacity_total, capacity_used=capacity_used,
        )
        s.add(acc)
        s.commit()
        s.refresh(acc)
        t = Task(taskname="剧集", shareurl="https://fake.example/s/1", savepath="/剧", account_id=acc.id)
        s.add(t)
        s.commit()
        s.refresh(t)
        return acc, t


def _collector():
    lines: list[str] = []

    async def fake_push(title, content, push_config, settings, log):
        lines.append(content)

    return lines, fake_push


# ---------------------------------------------------------------- 磁盘空间预检


@pytest.mark.asyncio
async def test_download_blocked_when_disk_full(monkeypatch):
    """磁盘装不下：不开始下载、账本留 failed、原因写明磁盘空间不足。"""
    # 100GB 盘只剩 2GB，要下 10GB（+10% 余量 = 11GB）
    monkeypatch.setattr(
        resource_guard.shutil,
        "disk_usage",
        lambda p: SimpleNamespace(total=100 * GB, free=2 * GB, used=98 * GB),
    )
    cfg = DownloadSettings.from_dict({"mode": "builtin", "dir": "/tmp/xp-guard-test"})
    items = [DownloadItem(fid="1", name="big.mp4", size=10 * GB, local_path=Path("/tmp/xp-guard-test/big.mp4"))]

    lines = await download_service.download_items(
        OkDriver(), items, cfg, log=lambda level, msg: None,
        task_id=None, taskname="剧集", account_id=None, driver_key="fake",
    )
    assert any("磁盘空间不足" in line for line in lines), lines

    data = await asyncio.to_thread(download_history.list_records, page=1, page_size=20, keyword="big.mp4")
    hit = [r for r in data["items"] if r["status"] == "failed" and "磁盘空间不足" in (r["error"] or "")]
    assert hit, "预检拦截必须在账本里留痕，否则用户只看到'没下载'"


@pytest.mark.asyncio
async def test_download_proceeds_when_disk_enough(monkeypatch):
    """空间充足时不得误拦。"""
    monkeypatch.setattr(
        resource_guard.shutil,
        "disk_usage",
        lambda p: SimpleNamespace(total=500 * GB, free=400 * GB, used=100 * GB),
    )
    ok, why = resource_guard.check_disk_space(Path("/tmp/xp-guard-test"), 10 * GB)
    assert ok is True and why == ""


def test_disk_unknown_does_not_block(monkeypatch):
    """读不到磁盘信息时不阻断：这是防呆不是拦截，误判比不判更糟。"""

    def boom(path):
        raise OSError("no such device")

    monkeypatch.setattr(resource_guard.shutil, "disk_usage", boom)
    ok, _ = resource_guard.check_disk_space(Path("/tmp"), 10 * GB)
    assert ok is True


# ---------------------------------------------------------------- 网盘配额预检


@pytest.mark.asyncio
async def test_quota_nearly_full_skips_transfer(monkeypatch):
    """剩余容量低于 2%：不再转存，并说明原因。"""
    _, _task = _seed(capacity_total=100 * GB, capacity_used=99 * GB)  # 剩 1GB < 2GB
    lines, fake_push = _collector()
    monkeypatch.setattr(task_service, "route_driver", lambda url: OkDriver)
    monkeypatch.setattr(task_service, "_push", fake_push)

    summary = await task_service.run_tasks(trigger="manual")
    assert summary["failed"] == 1
    assert any("网盘容量不足" in x for x in lines), lines


@pytest.mark.asyncio
async def test_quota_unknown_does_not_block(monkeypatch):
    """容量未知（total=0）时不拦：不知道就不能假装知道。"""
    _seed(capacity_total=0, capacity_used=0)
    lines, fake_push = _collector()
    monkeypatch.setattr(task_service, "route_driver", lambda url: OkDriver)
    monkeypatch.setattr(task_service, "_push", fake_push)

    summary = await task_service.run_tasks(trigger="manual")
    assert summary["updated"] == 1, lines


# ---------------------------------------------------------------- 限流退避


@pytest.mark.asyncio
async def test_rate_limit_sets_backoff_then_skips_next_round(monkeypatch):
    """连续风控：记退避，下一轮该账号任务整轮跳过，而不是继续硬撞。"""
    acc, _task = _seed()
    lines, fake_push = _collector()

    async def boom(driver, spec, magic_regex=None, log=None):
        raise RuntimeError("触发风控，请稍后再试")

    monkeypatch.setattr(task_service, "run_update_task", boom)
    monkeypatch.setattr(task_service, "route_driver", lambda url: OkDriver)
    monkeypatch.setattr(task_service, "_push", fake_push)

    await task_service.run_tasks(trigger="manual")  # run_tasks 自己吞异常，不向外抛
    blocked, why = resource_guard.is_in_backoff(acc.id)
    assert blocked, "限流必须记入退避账本"
    assert "退避" in why

    # 下一轮：该账号在退避期，任务被跳过且不再调用驱动
    def should_not_run(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("退避期内不应调用 run_update_task")

    monkeypatch.setattr(task_service, "run_update_task", should_not_run)
    summary = await task_service.run_tasks(trigger="manual")
    assert summary["skipped"] == 1
    assert any("退避" in x for x in lines), lines


@pytest.mark.asyncio
async def test_success_clears_backoff(monkeypatch):
    """退避已过期、又正常跑通一次 → 清掉残留记录，不让 fail_count 长期累积。"""
    acc, _task = _seed()
    resource_guard.note_failure(acc.id, "限流")
    # 退避期内任务本就被跳过，所以"成功清零"只发生在过期后的第一次成功运行
    with session_scope() as s:
        row = s.get(DriveBackoff, acc.id)
        row.until_at = datetime.now() - timedelta(seconds=1)
        s.add(row)
    assert resource_guard.is_in_backoff(acc.id)[0] is False, "已过期的退避不应再阻塞"

    async def ok_run(driver, spec, magic_regex=None, log=None):
        return SimpleNamespace(status="no_changes", message="", files=[], render=lambda: "")

    monkeypatch.setattr(task_service, "run_update_task", ok_run)
    monkeypatch.setattr(task_service, "route_driver", lambda url: OkDriver)
    lines, fake_push = _collector()
    monkeypatch.setattr(task_service, "_push", fake_push)

    await task_service.run_tasks(trigger="manual")
    with session_scope() as s:
        assert s.get(DriveBackoff, acc.id) is None


def test_backoff_grows_and_caps():
    assert resource_guard.backoff_seconds(1) == 60
    assert resource_guard.backoff_seconds(2) == 120
    assert resource_guard.backoff_seconds(3) == 240
    assert resource_guard.backoff_seconds(20) == 1800  # 上限 30 分钟


def test_rate_limit_detection():
    assert resource_guard.looks_like_rate_limit("触发风控，请稍后再试")
    assert resource_guard.looks_like_rate_limit("操作频繁")
    assert not resource_guard.looks_like_rate_limit("分享已被取消")
