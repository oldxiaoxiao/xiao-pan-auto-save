"""FR-09 总览：一屏回答「追更还好吗」。

设计前提：判断系统健康原本要跨任务/账号/下载/日志四个页面拼信息，而每个页面都只讲自己的
那一段。总览不新增任何判定逻辑，只把 FR-01~FR-08 已经算好的结论聚到一处并定级。

刻意不做的事：
- **不自己做判定**——健康度来自 `task_health`，凭据状态来自 `account_service`，磁盘来自
  `resource_guard`。这里若再算一遍，两边口径迟早漂移。
- **不做自动修复**——总览只回答"现在怎么样"，要动手去对应页面。
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import func
from sqlmodel import col, select

from ..database import session_scope
from ..models import Account, DownloadRecord, Task, TaskRun

# 总览结论：能一眼看出"要紧吗"，而不是让用户自己数红点
LEVEL_OK = "ok"
LEVEL_ATTENTION = "attention"
LEVEL_CRITICAL = "critical"


def _counts() -> dict:
    with session_scope() as s:
        total = s.exec(select(func.count()).select_from(Task)).one()
        active = s.exec(select(func.count()).select_from(Task).where(Task.disabled.is_(False))).one()
        accounts = s.exec(select(func.count()).select_from(Account)).one()
        enabled_accounts = s.exec(select(func.count()).select_from(Account).where(Account.enabled)).one()
    return {
        "tasks": int(total),
        "active_tasks": int(active),
        "accounts": int(accounts),
        "enabled_accounts": int(enabled_accounts),
    }


def _download_stats() -> dict:
    """按状态聚合账本。今日口径按 finished_at 落今天算。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as s:
        rows = s.exec(select(DownloadRecord.status, func.count()).group_by(DownloadRecord.status)).all()
        by_status = {st: int(n) for st, n in rows}
        done_today = s.exec(
            select(func.count())
            .select_from(DownloadRecord)
            .where(DownloadRecord.status == "done", DownloadRecord.finished_at >= today)
        ).one()
        failed_today = s.exec(
            select(func.count())
            .select_from(DownloadRecord)
            .where(DownloadRecord.status == "failed", DownloadRecord.finished_at >= today)
        ).one()
    return {
        "in_flight": by_status.get("queued", 0) + by_status.get("downloading", 0),
        "interrupted": by_status.get("interrupted", 0),
        "failed": by_status.get("failed", 0),
        "done": by_status.get("done", 0),
        "done_today": int(done_today),
        "failed_today": int(failed_today),
    }


def _last_run() -> dict | None:
    """最近一次运行结论：没有账本就没有——不拿任务配置去猜。"""
    with session_scope() as s:
        row = s.exec(select(TaskRun).order_by(col(TaskRun.id).desc())).first()
        if row is None:
            return None
        task = s.get(Task, row.task_id)
        return {
            "task_id": row.task_id,
            "taskname": task.taskname if task else f"#{row.task_id}",
            "status": row.status,
            "message": row.message,
            "at": row.created_at.isoformat(timespec="seconds"),
        }


def _disk(target: Path) -> dict:
    from .resource_guard import check_disk_space, fmt_bytes

    try:
        usage = shutil.disk_usage(str(target))
    except OSError:
        # 路径不存在或权限问题：如实说不知道，不编一个数字出来
        return {"known": False, "target": str(target), "reason": "无法读取磁盘信息"}
    ok, why = check_disk_space(target, 0)
    return {
        "known": True,
        "target": str(target),
        "free": usage.free,
        "total": usage.total,
        "free_text": fmt_bytes(usage.free),
        "total_text": fmt_bytes(usage.total),
        "used_pct": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        "will_block": not ok,
        "note": why,
    }


def build(download_dir: Path) -> dict:
    """聚出总览。调用方负责解析下载目录（路由层读设置，本函数不碰 setting）。"""
    from .account_service import invalid_accounts
    from .task_health import issues

    issues_list = issues()
    bad_accounts = invalid_accounts()
    counts = _counts()
    downloads = _download_stats()

    # 定级：需要动手的（账号失效、下载中断/失败、磁盘不够）算 critical，
    # 只是有任务待观察（连续失败、停摆）算 attention。
    blocking: list[str] = []
    if bad_accounts:
        blocking.append(f"{len(bad_accounts)} 个账号需更新 Cookie")
    if downloads["interrupted"]:
        blocking.append(f"{downloads['interrupted']} 个下载被中断（可继续）")
    if downloads["failed_today"]:
        blocking.append(f"今日 {downloads['failed_today']} 个下载失败")
    disk = _disk(download_dir)
    if disk.get("will_block"):
        blocking.append("磁盘可用空间低于预留")

    if blocking:
        level = LEVEL_CRITICAL
    elif issues_list:
        level = LEVEL_ATTENTION
    else:
        level = LEVEL_OK

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "level": level,
        "blocking": blocking,
        "counts": counts,
        "issues": issues_list[:8],
        "issues_total": len(issues_list),
        "bad_accounts": [
            {"id": a.get("id"), "name": a.get("name") or f"#{a.get('id')}", "message": a.get("message", "")}
            for a in bad_accounts
        ],
        "downloads": downloads,
        "disk": disk,
        "last_run": _last_run(),
    }
