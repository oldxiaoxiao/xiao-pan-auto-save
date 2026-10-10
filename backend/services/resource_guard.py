"""运行前资源预检（主方案 FR-01）：磁盘空间、网盘配额、限流退避。

三件事同一个目的：把「写到一半才发现不行」变成「开始前就知道不行」——
磁盘写满、网盘容量见底、账号被限流，这三种中断过去都要靠用户自己翻日志才发现。
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import select

from ..database import session_scope
from ..models import DriveBackoff

# 额外余量：aria2 会预分配整片、内置会先写 .part，按需求 110% 估，避免"刚好够"却写崩
SAFETY_MARGIN = 0.10
# 磁盘预留：占总容量 10%，但上限 5GB——1TB 盘要求预留 100GB 太保守，反而什么都下不了
DEFAULT_RESERVE_PCT = 0.10
DEFAULT_RESERVE_CAP = 5 * 1024**3
# 网盘剩余低于 2% 就不再转存（本次大小在转存前未知，只能做粗粒度防呆）
QUOTA_MIN_FREE_PCT = 0.02

RATE_LIMIT_HINTS = ("限流", "风控", "操作频繁", "频繁", "429", "too many", "rate limit", "risk", "稍后再试")


def fmt_bytes(num: int) -> str:
    num = int(num or 0)
    if num <= 0:
        return "0"
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(num)
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.0f} {units[i]}" if i == 0 else f"{v:.1f} {units[i]}"


def _anchor(target: Path) -> Path:
    """目标目录可能还不存在（首次下载），向上找最近存在的祖先来取磁盘用量。"""
    p = Path(target)
    try:
        while not p.exists() and p.parent != p:
            p = p.parent
    except OSError:
        return Path(target)
    return p if p.exists() else Path(target)


def check_disk_space(
    target: Path,
    need_bytes: int,
    reserve_pct: float = DEFAULT_RESERVE_PCT,
    reserve_cap: int = DEFAULT_RESERVE_CAP,
) -> tuple[bool, str]:
    """下载开始前判断目标盘装不装得下。装不下返回 (False, 原因)。"""
    need = int((need_bytes or 0) * (1 + SAFETY_MARGIN))
    if need <= 0:
        return True, ""
    anchor = _anchor(target)
    try:
        usage = shutil.disk_usage(anchor)
    except OSError as exc:
        # 读不到磁盘信息不阻断：这是防呆不是拦截，误判比不判更糟
        return True, f"磁盘用量读取失败（{exc}），本次不做空间预检"
    reserve = min(int(usage.total * reserve_pct), reserve_cap)
    free_after = usage.free - need
    if free_after < reserve:
        return False, (
            f"磁盘空间不足：本次需要 {fmt_bytes(need)}（含 10% 余量），"
            f"当前可用 {fmt_bytes(usage.free)}，下载后仅剩 {fmt_bytes(max(free_after, 0))}，"
            f"低于预留 {fmt_bytes(reserve)}"
        )
    return True, ""


def check_quota(used: int, total: int, need_bytes: int = 0) -> tuple[bool, str]:
    """网盘配额预检。容量未知（total<=0）时不阻断——不知道就不能假装知道。"""
    total = int(total or 0)
    if total <= 0:
        return True, ""
    free = total - int(used or 0)
    floor = int(total * QUOTA_MIN_FREE_PCT)
    need = int(need_bytes or 0)
    if free - need < floor:
        return False, (
            f"网盘容量不足：可用 {fmt_bytes(max(free, 0))}，"
            f"本次需要 {fmt_bytes(need)}，低于 {int(QUOTA_MIN_FREE_PCT * 100)}% 预留 {fmt_bytes(floor)}"
        )
    return True, ""


def looks_like_rate_limit(message: str) -> bool:
    low = (message or "").lower()
    return any(h.lower() in low for h in RATE_LIMIT_HINTS)


def backoff_seconds(fail_count: int) -> int:
    """指数退避：60s → 120s → 240s…，上限 30 分钟。"""
    return min(60 * 2 ** max(0, int(fail_count) - 1), 1800)


def is_in_backoff(account_id: int) -> tuple[bool, str]:
    with session_scope() as session:
        row = session.get(DriveBackoff, account_id)
        if row is None or row.until_at is None:
            return False, ""
        if row.until_at <= datetime.now():
            return False, ""
        left = int((row.until_at - datetime.now()).total_seconds())
        return True, f"账号处于退避期（{row.reason}），还需等待 {left}s"


def note_failure(account_id: int, reason: str) -> int:
    """记一次限流/风控失败，返回本次退避秒数。非限流错误不记（避免正常失败误伤）。"""
    with session_scope() as session:
        row = session.get(DriveBackoff, account_id)
        if row is None:
            row = DriveBackoff(account_id=account_id)
        row.fail_count = int(row.fail_count or 0) + 1
        row.reason = (reason or "")[:200]
        row.until_at = datetime.now() + timedelta(seconds=backoff_seconds(row.fail_count))
        row.updated_at = datetime.now()
        session.add(row)
        return backoff_seconds(row.fail_count)


def clear_backoff(account_id: int) -> None:
    """成功一次就清零：退避是给连续失败用的，不能让一次限流永久影响账号。"""
    with session_scope() as session:
        row = session.get(DriveBackoff, account_id)
        if row is not None:
            session.delete(row)


def backoff_snapshot() -> list[dict]:
    with session_scope() as session:
        rows = session.exec(select(DriveBackoff)).all()
    return [
        {
            "account_id": r.account_id,
            "fail_count": r.fail_count,
            "until_at": r.until_at.isoformat(timespec="seconds") if r.until_at else None,
            "reason": r.reason,
        }
        for r in rows
        if r.until_at and r.until_at > datetime.now()
    ]
