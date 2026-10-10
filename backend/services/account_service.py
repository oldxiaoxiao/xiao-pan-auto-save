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
    from .credential_store import plain_cookie

    return cls(cookie=plain_cookie(account) or "", proxy=PROXY, index=index)


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
            invalid_reason = "" if info.valid else "Cookie 已失效，请更新后重试"
            with session_scope() as session:
                row = session.get(Account, acc.id)
                if row:
                    row.nickname = info.nickname or row.nickname
                    row.capacity_total = info.total
                    row.member_type = info.member_type
                    row.can_save = info.can_save
                    row.last_check_at = datetime.now()
                    # FR-02：把"这次到底有效没效"落库，界面与调度都靠它判断，
                    # 不能只靠 can_save——can_save 还会被会员类型等因素影响，语义不纯。
                    row.check_ok = bool(info.valid)
                    row.check_message = invalid_reason
                    session.add(row)
            results.append(
                {"id": acc.id, "ok": info.valid, "nickname": info.nickname, "message": invalid_reason}
            )
            log(
                "info",
                f"账号[{acc.name or acc.id}] {info.nickname or ''} {'有效' if info.valid else 'Cookie已失效'}",
            )
        except Exception as exc:  # noqa: BLE001 检查失败不应中断
            # 检查异常也算"没通过"，但原因要如实写，不能谎报成 Cookie 失效
            with session_scope() as session:
                row = session.get(Account, acc.id)
                if row:
                    row.check_ok = False
                    row.check_message = f"健康检查失败：{exc}"[:200]
                    # 也要记检查时间：last_check_at 是"是否真的查过"的判据，
                    # 不写就会把这次失败降级成"没查过"，界面与调度都不再提示。
                    row.last_check_at = datetime.now()
                    session.add(row)
            results.append({"id": acc.id, "ok": False, "message": str(exc)})
            log("warn", f"账号[{acc.name or acc.id}] 检查异常：{exc}")
        finally:
            await drv.close()
    return results


def invalid_accounts() -> list[dict]:
    """当前处于失效状态的启用账号（界面徽标与提醒的唯一出处）。

    必须同时要求 last_check_at 非空：从没检查过的账号 check_ok 是默认值，
    不能因为"没查过"就被判成失效（补列默认值、新账号都是这种情况）。
    """
    with session_scope() as session:
        rows = session.exec(
            select(Account)
            .where(Account.enabled, Account.check_ok.is_(False), Account.last_check_at.is_not(None))
            .order_by(Account.id)
        ).all()
    return [
        {
            "id": a.id,
            "name": a.name or f"账号{a.id}",
            "driver_key": a.driver_key,
            "message": a.check_message or "账号不可用",
            "last_check_at": a.last_check_at.isoformat(timespec="seconds") if a.last_check_at else None,
        }
        for a in rows
    ]


async def alert_invalid_accounts(log=None, *, min_interval_hours: int = 24) -> int:
    """FR-02：失效账号推一次「需处理」提醒，24 小时内不重复骚扰。

    返回本次实际推送的条数（0 表示都在静默期内或没有失效账号）。
    """
    log = log or hub.make_logger("accounts")
    rows = invalid_accounts()
    if not rows:
        return 0
    from datetime import timedelta

    from ..api.deps import get_setting
    from .notify_center import LEVEL_ACTION, dispatch

    settings = {
        "notify_enabled": bool(get_setting("notify_enabled")),
        "notify_quiet": get_setting("notify_quiet"),
    }
    push_config = get_setting("push_config") or {}
    now = datetime.now()
    due: list[dict] = []
    with session_scope() as session:
        for r in rows:
            acc = session.get(Account, r["id"])
            if acc is None:
                continue
            if acc.invalid_notified_at and now - acc.invalid_notified_at < timedelta(hours=min_interval_hours):
                continue
            acc.invalid_notified_at = now
            session.add(acc)
            due.append(r)
    if not due:
        return 0
    lines = [f"• {r['name']}（{r['driver_key']}）：{r['message']}" for r in due]
    content = "以下网盘账号需要你处理，相关任务已暂停执行：\n" + "\n".join(lines)
    try:
        # FR-04：走分级通道。需处理级在免打扰时段不丢——攒进队列，次日随摘要补发。
        await dispatch(
            [(LEVEL_ACTION, content)],
            settings=settings,
            push_config=push_config,
            log=log,
            title="网盘账号需要更新",
        )
    except Exception as exc:  # noqa: BLE001 推送失败也要保住已更新的通知时间
        log("warn", f"失效账号提醒推送失败：{exc}")
        return 0
    log("warn", f"已提醒 {len(due)} 个失效账号")
    return len(due)


def mark_cookie_refreshed(account_id: int) -> None:
    """用户重新录入 Cookie 后清除失效状态：徽标消失、任务恢复执行。"""
    with session_scope() as session:
        row = session.get(Account, account_id)
        if row is None:
            return
        row.check_ok = True
        row.check_message = ""
        row.invalid_notified_at = None
        session.add(row)


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
