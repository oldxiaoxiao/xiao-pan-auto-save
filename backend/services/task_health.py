"""任务健康度与失败归因（FR-03）。

过去"这个任务到底还正常吗"只能靠翻日志，现在把判定收成一个函数：
正常 / 待处理 / 停摆，并给出具体原因。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import select

from ..database import session_scope
from ..models import Account, Task, TaskRun

FAIL_STREAK_THRESHOLD = 3  # 连续失败几次算待处理
STALE_DAYS = 3  # 定时追更任务多久没跑过算停摆

# 只有这些状态算"这次没成功"，no_changes 是正常的（本来就没更新）
FAIL_STATUSES = ("failed", "banned", "network", "quota", "error")


def _streak_and_reason(task_id: int, threshold: int = FAIL_STREAK_THRESHOLD) -> tuple[int, str]:
    """最近连续失败次数与最近一次失败原因。"""
    with session_scope() as session:
        rows = session.exec(
            select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.id.desc()).limit(threshold)
        ).all()
    streak = 0
    reason = ""
    for row in rows:
        if row.status in FAIL_STATUSES:
            streak += 1
            if not reason:
                reason = row.message or row.status
        else:
            break
    return streak, reason


def compute_health(task: Task) -> dict:
    """单个任务的健康判定。返回 {status, reason, fail_streak, last_status}。

    status：ok=正常 / attention=待处理 / stale=停摆
    """
    with session_scope() as session:
        last = session.exec(
            select(TaskRun).where(TaskRun.task_id == task.id).order_by(TaskRun.id.desc())
        ).first()
    last_status = last.status if last else ""

    # 1) 分享已失效：最硬的信号，直接待处理
    if getattr(task, "shareurl_ban", ""):
        return {
            "status": "attention",
            "reason": f"分享已失效：{task.shareurl_ban}",
            "fail_streak": 0,
            "last_status": last_status,
            "kind": "banned",
        }

    # 2) 关联账号失效：不是任务本身的错，但用户得先去更新账号
    acc_id = getattr(task, "account_id", None)
    if acc_id is not None:
        with session_scope() as session:
            acc = session.get(Account, acc_id)
        if acc is not None and acc.check_ok is False and acc.last_check_at is not None:
            return {
                "status": "attention",
                "reason": f"账号「{acc.name or acc.id}」{acc.check_message or '不可用'}，需更新 Cookie",
                "fail_streak": 0,
                "last_status": last_status,
                "kind": "account",
            }

    # 3) 连续失败
    streak, reason = _streak_and_reason(int(task.id or 0))
    if streak >= FAIL_STREAK_THRESHOLD:
        return {
            "status": "attention",
            "reason": reason or "连续多次运行失败",
            "fail_streak": streak,
            "last_status": last_status,
            "kind": "failing",
        }

    # 4) 定时追更却很久没跑：多半是调度没生效或程序没开
    last_run = getattr(task, "last_run_at", None)
    if last_run is None:
        return {"status": "ok", "reason": "", "fail_streak": streak, "last_status": last_status, "kind": ""}
    if last_run < datetime.now() - timedelta(days=STALE_DAYS) and getattr(task, "run_mode", "follow") == "follow":
        return {
            "status": "stale",
            "reason": f"已 {STALE_DAYS} 天以上没有运行，检查调度或程序是否在运行",
            "fail_streak": streak,
            "last_status": last_status,
            "kind": "stale",
        }

    return {"status": "ok", "reason": "", "fail_streak": streak, "last_status": last_status, "kind": ""}


def health_map() -> dict[int, dict]:
    with session_scope() as session:
        tasks = session.exec(select(Task).order_by(Task.sort_order, Task.id)).all()
    return {int(t.id or 0): compute_health(t) for t in tasks}


def issues() -> list[dict]:
    """待处理聚合视图：所有非正常任务 + 原因，按严重度排序。"""
    with session_scope() as session:
        tasks = session.exec(select(Task).order_by(Task.sort_order, Task.id)).all()
    out = []
    for t in tasks:
        h = compute_health(t)
        if h["status"] == "ok":
            continue
        out.append(
            {
                "id": t.id,
                "taskname": t.taskname,
                "shareurl": t.shareurl,
                "savepath": t.savepath,
                "disabled": t.disabled,
                "run_mode": getattr(t, "run_mode", "follow"),
                "last_run_at": t.last_run_at.isoformat(timespec="seconds") if t.last_run_at else None,
                **h,
            }
        )
    order = {"attention": 0, "stale": 1}
    out.sort(key=lambda x: (order.get(x["status"], 9), -(x["fail_streak"] or 0)))
    return out


def record_run(task_id: int, status: str, message: str = "", run_id: str = "") -> None:
    """运行结论落账本。写失败只记 warn，绝不影响运行本身。"""
    with session_scope() as session:
        session.add(TaskRun(task_id=task_id, run_id=run_id, status=status, message=(message or "")[:300]))


def recent_runs(task_id: int, limit: int = 10) -> list[dict]:
    with session_scope() as session:
        rows = session.exec(
            select(TaskRun).where(TaskRun.task_id == task_id).order_by(TaskRun.id.desc()).limit(limit)
        ).all()
    return [
        {
            "id": r.id,
            "status": r.status,
            "message": r.message,
            "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        }
        for r in rows
    ]
