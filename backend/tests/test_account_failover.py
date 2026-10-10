"""FR-06 账号容灾：主账号当下不可用时切到同驱动的可用账号，且切换必须留痕。"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlmodel import delete

from backend.database import session_scope
from backend.drivers.base import AccountInfo, CloudDrive, FsItem, SaveResult, ShareRef
from backend.models import Account, DriveBackoff, Task
from backend.services import task_service


class OkDriver(CloudDrive):
    key = "fake"
    name = "假盘"
    share_domains = ["fake.example"]
    supported = True
    capability = {"account", "rename"}

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

    async def account_info(self):
        return AccountInfo(nickname="有效用户", total=100, valid=True, can_save=True)

    async def close(self):
        pass


@pytest.fixture(autouse=True)
def clean_db():
    # 退避表也要清：SQLite 删空后新行会复用旧 id，上一例给某账号记的退避会"附身"到下一例的账号上
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(Account))
        s.exec(delete(DriveBackoff))
    yield
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(Account))
        s.exec(delete(DriveBackoff))


def _account(name: str, *, enabled=True, driver_key="fake", sort_order=0):
    with session_scope() as s:
        acc = Account(
            driver_key=driver_key,
            cookie="ck",
            enabled=enabled,
            can_save=True,
            name=name,
            sort_order=sort_order,
        )
        s.add(acc)
        s.commit()
        s.refresh(acc)
        return acc.id, name


def _task(account_id=None, **kw):
    with session_scope() as s:
        t = Task(taskname="剧集", shareurl="https://fake.example/s/1", savepath="/剧", **kw)
        if account_id:
            t.account_id = account_id
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


def _expire(acc_id: int, msg="Cookie 已失效，请更新后重试"):
    with session_scope() as s:
        row = s.get(Account, acc_id)
        row.check_ok = False
        row.check_message = msg
        row.last_check_at = datetime.now()  # 必须"真查过"，否则按未知处理不拦截
        s.add(row)


async def _run(monkeypatch):
    """跑一轮任务，返回 (摘要, 推送内容列表)。"""
    lines: list[str] = []

    async def fake_push(title, content, push_config, settings, log):
        lines.append(content)

    monkeypatch.setattr(task_service, "route_driver", lambda url: OkDriver)
    monkeypatch.setattr(task_service, "_push", fake_push)
    summary = await task_service.run_tasks(trigger="manual")
    return summary, lines


# ------------------------------------------------------------ 未绑定账号：直接顺延


@pytest.mark.asyncio
async def test_unbound_task_uses_healthy_account_when_first_is_expired(monkeypatch):
    """没绑定账号的任务没有"必须用哪个号"的语义，主号失效就该顺延到下一个。"""
    bad, _ = _account("主号", sort_order=0)
    good, _ = _account("备号", sort_order=1)
    _expire(bad)
    _task()

    summary, lines = await _run(monkeypatch)
    assert summary["updated"] == 1, lines
    assert not any("已失效" in x for x in lines), "有可用账号时不该报失效"

    with session_scope() as s:
        assert s.get(Account, good).check_ok is True


# ------------------------------------------------------------ 绑定账号：容灾切换


@pytest.mark.asyncio
async def test_bound_account_failover_to_healthy_one(monkeypatch):
    """绑定账号失效 + 存在同驱动健康账号 → 任务照跑，不因为一个号挂了就停摆。"""
    bad, _ = _account("主号", sort_order=0)
    _account("备号", sort_order=1)
    _expire(bad)
    _task(account_id=bad)

    summary, lines = await _run(monkeypatch)
    assert summary["updated"] == 1, lines
    assert summary["failed"] == 0
    joined = "\n".join(lines)
    assert "备号" in joined and "已临时切到" in joined, "切换必须留痕，否则用户找不到文件"


@pytest.mark.asyncio
async def test_failover_disabled_strictly_keeps_bound_account(monkeypatch):
    """关掉容灾=严格只用绑定的号：宁可失败也不换号（用户可能是有意为之）。"""

    def boom(*args, **kwargs):  # pragma: no cover - 被调用即说明没拦住
        raise AssertionError("关掉容灾后不应切到别的账号")

    bad, _ = _account("主号", sort_order=0)
    _account("备号", sort_order=1)
    _expire(bad)
    _task(account_id=bad, account_failover=False)

    monkeypatch.setattr(task_service, "route_driver", boom)
    summary, lines = await _run(monkeypatch)
    assert summary["failed"] == 1
    joined = "\n".join(lines)
    assert "Cookie 已失效" in joined
    assert "切到" not in joined


@pytest.mark.asyncio
async def test_no_failover_when_all_accounts_expired(monkeypatch):
    """没有可顶替的账号时，失败原因要保留原有口径，不能退化成"缺少账号"。"""
    bad, _ = _account("主号")
    _expire(bad)
    _task(account_id=bad)

    summary, lines = await _run(monkeypatch)
    assert summary["failed"] == 1
    joined = "\n".join(lines)
    assert "Cookie 已失效" in joined
    assert "未配置可用" not in joined, "有账号只是失效了，不能谎报成没配账号"
    assert "切到" not in joined


@pytest.mark.asyncio
async def test_failover_never_crosses_driver(monkeypatch):
    """夸克的分享只能用夸克账号：其它驱动的账号再健康也不能顶上。"""
    bad, _ = _account("主号", driver_key="fake")
    _account("别的盘", driver_key="other")
    _expire(bad)
    _task(account_id=bad)

    summary, lines = await _run(monkeypatch)
    assert summary["failed"] == 1
    assert "Cookie 已失效" in "\n".join(lines)


# ------------------------------------------------------------ 退避期同样让位


@pytest.mark.asyncio
async def test_backoff_account_yields_to_healthy_one(monkeypatch):
    """账号在风控退避期 = 此刻干不了活，未绑定的任务应顺延到健康的号。"""
    from backend.services import resource_guard

    cooling, _ = _account("被风控的号", sort_order=0)
    _account("备号", sort_order=1)
    resource_guard.note_failure(cooling, "触发风控")
    _task()

    summary, lines = await _run(monkeypatch)
    assert summary["updated"] == 1, lines
    assert not any("本轮跳过" in x for x in lines)


# ------------------------------------------------------------ 可用账号的判定


def test_account_usable_reports_reason_and_kind():
    """不可用的原因要能分类：调用方靠它决定通知级别。"""
    from backend.services.account_failover import account_usable

    acc_id, _ = _account("主号")
    with session_scope() as s:
        acc = s.get(Account, acc_id)
        assert account_usable(acc)[0] is True

    _expire(acc_id)
    with session_scope() as s:
        acc = s.get(Account, acc_id)
        ok, why, kind = account_usable(acc)
        assert ok is False and kind == "invalid" and "失效" in why
