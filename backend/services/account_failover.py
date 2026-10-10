"""FR-06 账号容灾：主账号当下干不了活时，自动切到同驱动的另一个可用账号。

明确划定的边界（不做的部分不是没做，是刻意不做）：

- **只在"这个账号确实干不了活"时切换**：Cookie 失效、风控退避中、网盘配额见底、凭据解不开。
  转存失败本身**不**触发切换——分享链接失效换号也解决不了，切了只会掩盖真实原因。
- **不跨驱动切换**：夸克的分享链接只能用夸克账号，拿百度账号去转存没有意义。
- **切换必须留痕**：静默换号会让用户在原账号里找不到转存的文件，所以日志与通知都要写明
  「从 A 切到 B、原因是什么」。
- **任务可关闭**：绑定了特定账号的任务，用户可能是有意为之（比如某号空间充裕），
  所以按任务提供开关，关掉就严格只用绑定的那个号。
"""

from __future__ import annotations

from sqlmodel import select

from ..database import session_scope
from ..models import Account

# 账号不可用的原因文案，调用方直接拿去拼通知/日志
REASON_BACKOFF = "处于风控退避期"
REASON_INVALID = "Cookie 已失效"
REASON_QUOTA = "网盘空间不足"
REASON_CREDENTIAL = "凭据无法解密"


def _healthy(acc: Account) -> bool:
    """凭据失效判据与 FR-02 保持一致：必须真的检查过，没查过不算失效。"""
    return bool(getattr(acc, "check_ok", True)) or getattr(acc, "last_check_at", None) is None


def account_usable(acc: Account) -> tuple[bool, str, str]:
    """该账号此刻能不能干活。返回 (可用, 原因文案, 原因类别)。

    类别用于调用方决定通知级别与图标，文案直接给用户看。
    """
    from .credential_store import plain_cookie
    from .resource_guard import check_quota, is_in_backoff

    blocked, why = is_in_backoff(int(acc.id or 0))
    if blocked:
        return False, why or REASON_BACKOFF, "backoff"
    if not _healthy(acc):
        return False, (getattr(acc, "check_message", "") or REASON_INVALID), "invalid"
    quota_ok, quota_why = check_quota(
        getattr(acc, "capacity_used", 0) or 0,
        getattr(acc, "capacity_total", 0) or 0,
    )
    if not quota_ok:
        return False, quota_why or REASON_QUOTA, "quota"
    # 凭据解不开比"有 Cookie 但无效"更致命：驱动拿不到任何身份，请求必然失败。
    if plain_cookie(acc) is None:
        return False, REASON_CREDENTIAL, "credential"
    return True, "", ""


def usable_accounts(driver_key: str, exclude: set[int] | None = None) -> list[Account]:
    """同驱动下此刻可用的账号，按 sort_order 排列。"""
    exclude = exclude or set()
    with session_scope() as session:
        rows = session.exec(
            select(Account)
            .where(Account.enabled, Account.driver_key == driver_key)
            .order_by(Account.sort_order, Account.id)
        ).all()
        # session 关闭后对象会失效，先把判定做完再返回
        return [a for a in rows if int(a.id or 0) not in exclude and account_usable(a)[0]]  # noqa: FBT003


def pick_failover(driver_key: str, exclude: set[int] | None = None) -> Account | None:
    """挑一个可以顶上的账号；没有就返回 None（调用方按原逻辑报失败原因）。"""
    return next(iter(usable_accounts(driver_key, exclude)), None)
