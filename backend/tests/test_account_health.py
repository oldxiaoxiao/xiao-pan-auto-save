"""FR-02 账号凭据到期预警：检查结论落库、失效提醒 24h 去重、失效账号不空跑。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import delete

from backend.database import session_scope
from backend.drivers.base import AccountInfo, CloudDrive, FsItem, SaveResult, ShareRef
from backend.models import Account, Task
from backend.services import account_service, notify_service, task_service


class HealthyDriver(CloudDrive):
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

    async def close(self):
        pass


class ValidDriver(HealthyDriver):
    async def account_info(self):
        return AccountInfo(nickname="有效用户", total=100, valid=True, can_save=True)


class ExpiredDriver(HealthyDriver):
    async def account_info(self):
        return AccountInfo(nickname="", total=0, valid=False, can_save=False)


class BoomDriver(HealthyDriver):
    async def account_info(self):
        raise RuntimeError("网络不可达")


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
        acc = Account(driver_key=driver_key, cookie="ck", enabled=True, can_save=True, name="主号")
        s.add(acc)
        s.commit()
        s.refresh(acc)
        t = Task(taskname="剧集", shareurl="https://fake.example/s/1", savepath="/剧", account_id=acc.id)
        s.add(t)
        s.commit()
        s.refresh(t)
        return acc, t


def _logs():
    out: list[str] = []
    return out, lambda level, msg: out.append(f"{level}:{msg}")


# ------------------------------------------------------------ 检查结论要落库


@pytest.mark.asyncio
async def test_expired_cookie_is_recorded(monkeypatch):
    monkeypatch.setattr(account_service, "get_driver_class", lambda key: ExpiredDriver)
    acc, _ = _seed()

    results = await account_service.refresh_accounts(log=lambda level, msg: None)

    assert results[0]["ok"] is False
    with session_scope() as s:
        row = s.get(Account, acc.id)
        assert row.check_ok is False, "失效必须落库，界面徽标与调度都靠它判断"
        assert row.check_message
        assert row.last_check_at is not None
    assert account_service.invalid_accounts(), "失效账号应出现在待处理清单里"


@pytest.mark.asyncio
async def test_valid_cookie_stays_clean(monkeypatch):
    monkeypatch.setattr(account_service, "get_driver_class", lambda key: ValidDriver)
    acc, _ = _seed()

    await account_service.refresh_accounts(log=lambda level, msg: None)

    with session_scope() as s:
        row = s.get(Account, acc.id)
        assert row.check_ok is True
        assert row.check_message == ""
    assert account_service.invalid_accounts() == []


@pytest.mark.asyncio
async def test_check_error_not_reported_as_cookie_expired(monkeypatch):
    """检查请求失败也算没通过，但原因要如实写，不能谎报成 Cookie 失效。"""
    monkeypatch.setattr(account_service, "get_driver_class", lambda key: BoomDriver)
    _seed()

    await account_service.refresh_accounts(log=lambda level, msg: None)

    rows = account_service.invalid_accounts()
    assert rows and "健康检查失败" in rows[0]["message"]


# ------------------------------------------------------------ 提醒与 24h 去重


@pytest.mark.asyncio
async def test_alert_once_then_silent_within_24h(monkeypatch):
    """失效只提醒一次：24 小时内不重复骚扰。"""
    monkeypatch.setattr(account_service, "get_driver_class", lambda key: ExpiredDriver)
    acc, _ = _seed()
    await account_service.refresh_accounts(log=lambda level, msg: None)

    pushed: list[tuple[str, str]] = []

    async def fake_push(title, content, push_config, log=None):
        # 签名与 notify_service.push_all 真实签名一致（FR-04 修掉了多传 settings 的历史调用）
        pushed.append((title, content))

    monkeypatch.setattr(notify_service, "push_all", fake_push)

    first = await account_service.alert_invalid_accounts()
    assert first == 1 and len(pushed) == 1
    assert "需要更新" in pushed[0][0]

    second = await account_service.alert_invalid_accounts()
    assert second == 0, "24 小时内不得重复推送"
    assert len(pushed) == 1


@pytest.mark.asyncio
async def test_alert_again_after_24h(monkeypatch):
    monkeypatch.setattr(account_service, "get_driver_class", lambda key: ExpiredDriver)
    acc, _ = _seed()
    await account_service.refresh_accounts(log=lambda level, msg: None)

    async def fake_push(title, content, push_config, log=None):
        # 签名与 notify_service.push_all 真实签名一致（FR-04 修掉了多传 settings 的历史调用）
        return None

    monkeypatch.setattr(notify_service, "push_all", fake_push)

    assert await account_service.alert_invalid_accounts() == 1
    # 把上次通知时间推到 25 小时前，静默期应失效
    with session_scope() as s:
        row = s.get(Account, acc.id)
        row.invalid_notified_at = datetime.now() - timedelta(hours=25)
        s.add(row)
    assert await account_service.alert_invalid_accounts() == 1


# ------------------------------------------------------------ 更新后恢复


@pytest.mark.asyncio
async def test_new_cookie_clears_invalid_state(monkeypatch):
    """重新粘贴 Cookie → 徽标消失、任务恢复执行。"""
    monkeypatch.setattr(account_service, "get_driver_class", lambda key: ExpiredDriver)
    acc, _ = _seed()
    await account_service.refresh_accounts(log=lambda level, msg: None)
    assert account_service.invalid_accounts()

    monkeypatch.setattr(account_service, "get_driver_class", lambda key: ValidDriver)
    account_service.mark_cookie_refreshed(acc.id)  # 路由在写入新 Cookie 时调用

    with session_scope() as s:
        row = s.get(Account, acc.id)
        assert row.check_ok is True
        assert row.invalid_notified_at is None
    assert account_service.invalid_accounts() == []


# ------------------------------------------------------------ 失效账号不空跑


@pytest.mark.asyncio
async def test_tasks_of_expired_account_are_skipped(monkeypatch):
    """Cookie 失效后继续打接口必然失败还会撞风控，宁可明说'等你更新'。"""
    acc, _task = _seed()
    with session_scope() as s:
        row = s.get(Account, acc.id)
        row.check_ok = False
        row.check_message = "Cookie 已失效，请更新后重试"
        row.last_check_at = datetime.now()  # 必须"真查过"，否则按未知处理不拦截
        s.add(row)

    lines: list[str] = []

    async def fake_push(title, content, push_config, settings, log):
        lines.append(content)

    def should_not_transfer(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("失效账号不应再调用转存")

    monkeypatch.setattr(task_service, "route_driver", lambda url: ValidDriver)
    monkeypatch.setattr(task_service, "run_update_task", should_not_transfer)
    monkeypatch.setattr(task_service, "_push", fake_push)

    summary = await task_service.run_tasks(trigger="manual")
    assert summary["failed"] == 1
    assert any("Cookie 已失效" in x for x in lines), lines


@pytest.mark.asyncio
async def test_healthy_account_runs_normally(monkeypatch):
    """别误伤：check_ok=True 的账号照常跑。"""
    _acc, _task = _seed()
    lines: list[str] = []

    async def fake_push(title, content, push_config, settings, log):
        lines.append(content)

    monkeypatch.setattr(task_service, "route_driver", lambda url: ValidDriver)
    monkeypatch.setattr(task_service, "_push", fake_push)

    summary = await task_service.run_tasks(trigger="manual")
    assert summary["updated"] == 1, lines


@pytest.mark.asyncio
async def test_never_checked_account_is_not_treated_invalid(monkeypatch):
    """从没检查过的账号不得被当成失效：否则新加的账号一进来就被暂停。"""
    acc, _task = _seed()
    with session_scope() as s:
        row = s.get(Account, acc.id)
        row.check_ok = False  # 默认值/补列值，且 last_check_at 为空
        row.last_check_at = None
        s.add(row)

    assert account_service.invalid_accounts() == []

    lines: list[str] = []

    async def fake_push(title, content, push_config, settings, log):
        lines.append(content)

    monkeypatch.setattr(task_service, "route_driver", lambda url: ValidDriver)
    monkeypatch.setattr(task_service, "_push", fake_push)

    summary = await task_service.run_tasks(trigger="manual")
    assert summary["updated"] == 1, "没查过的账号不该被拦"


def test_model_default_is_healthy():
    """回归：check_ok 的模型默认值必须是 True。

    _auto_add_columns 补列时取的就是这个默认值——它若变成 False，
    所有存量账号会在升级后被补成"失效"，一夜之间全被标成需更新。
    """
    from backend.models import Account

    col = Account.__table__.columns["check_ok"]
    py_default = getattr(col.default, "arg", None) if col.default is not None else None
    assert py_default is True
    assert Account(driver_key="fake", cookie="x").check_ok is True
