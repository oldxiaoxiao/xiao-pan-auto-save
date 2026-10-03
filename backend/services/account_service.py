"""账号服务：健康检查（昵称/容量）与签到。所有启用账号签到，转存用任务选定账号。"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import select

from ..config import PROXY
from ..core.logstream import hub
from ..database import session_scope
from ..drivers import get_driver_class
from ..models import Account


def _make(account: Account, index: int):
    cls = get_driver_class(account.driver_key)
    if cls is None or not cls.supported:
        return None
    return cls(cookie=account.cookie, proxy=PROXY, index=index)


async def refresh_accounts(log=None) -> list[dict]:
    """账号健康检查：昵称/有效性/容量，写回 Account 行。"""
    log = log or hub.make_logger("accounts")
    results = []
    with session_scope() as session:
        accounts = session.exec(select(Account).where(Account.enabled).order_by(Account.sort_order)).all()
        rows = [a for a in accounts]
    for i, acc in enumerate(rows):
        drv = _make(acc, i)
        if drv is None:
            results.append({"id": acc.id, "ok": False, "message": "驱动未实现"})
            continue
        try:
            info = await drv.account_info()
            with session_scope() as session:
                row = session.get(Account, acc.id)
                if row:
                    row.nickname = info.nickname or row.nickname
                    row.capacity_total = info.total
                    row.member_type = info.member_type
                    row.can_save = info.can_save
                    row.last_check_at = datetime.now()
                    session.add(row)
            results.append({"id": acc.id, "ok": info.valid, "nickname": info.nickname, "message": ""})
            log(
                "info",
                f"账号[{acc.name or acc.id}] {info.nickname or ''} {'有效' if info.valid else 'Cookie已失效'}",
            )
        except Exception as exc:  # noqa: BLE001 检查失败不应中断
            results.append({"id": acc.id, "ok": False, "message": str(exc)})
            log("warn", f"账号[{acc.name or acc.id}] 检查异常：{exc}")
        finally:
            await drv.close()
    return results


async def sign_accounts(log=None) -> list[dict]:
    """全部启用账号执行签到（驱动无签到能力则跳过）。"""
    log = log or hub.make_logger("sign")
    results = []
    with session_scope() as session:
        accounts = session.exec(select(Account).where(Account.enabled).order_by(Account.sort_order)).all()
        rows = [a for a in accounts]
    for i, acc in enumerate(rows):
        drv = _make(acc, i)
        if drv is None or not drv.has("sign"):
            continue
        try:
            res = await drv.sign()
            with session_scope() as session:
                row = session.get(Account, acc.id)
                if row:
                    row.last_sign_at = datetime.now()
                    row.sign_message = res.message
                    session.add(row)
            results.append({"id": acc.id, "ok": res.ok, "message": res.message, "reward": res.reward_bytes})
            log("info", f"账号[{acc.name or acc.id}] 签到：{res.message}")
        except Exception as exc:  # noqa: BLE001
            results.append({"id": acc.id, "ok": False, "message": str(exc)})
            log("warn", f"账号[{acc.name or acc.id}] 签到异常：{exc}")
        finally:
            await drv.close()
    return results
