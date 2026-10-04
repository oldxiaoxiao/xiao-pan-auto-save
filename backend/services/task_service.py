"""任务编排：账号选取、引擎调用、结果聚合通知。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from sqlmodel import select

from ..config import PROXY
from ..core.engine import TaskSpec, run_update_task
from ..core.logstream import hub
from ..core.router import route_driver
from ..core.scheduler import has_valid_schedule, task_due_today
from ..database import session_scope
from ..models import Account, Task

STATUS_ICONS = {
    "updated": "✅",
    "no_changes": None,
    "banned": "❌",
    "network": "⚠️",
    "failed": "❌",
}

# 全局运行锁：Phase 2 后多个 per-task 定时作业可并发调用 run_tasks；SQLite 无 WAL/busy_timeout
# 且同一夸克账号不宜被并发打。转存段（engine save + DB 写入）必须串行，下载段（本地/aria2）可并行，
# 故仅把转存段包进此锁。见 _run_tasks_inner。
_run_lock = asyncio.Lock()


def load_tasks(task_ids: list[int] | None = None) -> list[Task]:
    with session_scope() as session:
        stmt = select(Task).order_by(Task.sort_order, Task.id)
        if task_ids:
            stmt = stmt.where(Task.id.in_(task_ids))
        return list(session.exec(stmt).all())


def _task_spec(task: Task) -> TaskSpec:
    return TaskSpec(
        taskname=task.taskname,
        shareurl=task.shareurl,
        savepath=task.savepath,
        pattern=task.pattern,
        replace=task.replace,
        ignore_extension=task.ignore_extension,
        startfid=task.startfid,
        update_subdir=task.update_subdir,
        update_subdir_resave=task.update_subdir_resave,
        episode_start=task.episode_start,
        episode_end=task.episode_end,
        quality=task.quality,
    )


def _pick_account(tasks_account_id: int | None, driver_key: str) -> Account | None:
    """任务指定账号优先，否则取第一个启用且匹配驱动、有转存权限的账号。"""
    with session_scope() as session:
        if tasks_account_id:
            acc = session.get(Account, tasks_account_id)
            return acc if acc and acc.enabled else None
        accounts = session.exec(
            select(Account)
            .where(Account.enabled, Account.driver_key == driver_key)
            .order_by(Account.sort_order)
        ).all()
        return next((a for a in accounts if a.can_save), accounts[0] if accounts else None)


async def run_tasks(task_ids: list[int] | None = None, trigger: str = "manual") -> dict:
    """运行任务（全部或指定），聚合结果推送通知。返回摘要供 SSE/API 消费。"""
    run_id = uuid.uuid4().hex[:8]
    log = hub.make_logger(run_id)
    notify_lines: list[str] = []
    summary: dict = {
        "run_id": run_id,
        "trigger": trigger,
        "total": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
    }

    drivers: dict[int, object] = {}
    try:
        try:
            await _run_tasks_inner(run_id, log, drivers, notify_lines, summary, task_ids, trigger)
        finally:
            for drv in drivers.values():
                await drv.close()  # type: ignore[attr-defined]
            hub.publish("done", f"运行结束 run_id={run_id}", run_id=run_id)
    except Exception as exc:
        log("error", f"运行中断：{exc!r}")
    summary["notify_lines"] = len(notify_lines)
    return summary


async def _run_tasks_inner(
    run_id: str,
    log,
    drivers: dict[int, object],
    notify_lines: list[str],
    summary: dict,
    task_ids: list[int] | None,
    trigger: str,
) -> None:
    from ..api.deps import all_settings  # 局部导入避免环依赖

    settings = all_settings()
    magic_regex = settings.get("magic_regex") or {}
    push_config = settings.get("push_config") or {}
    tasks = load_tasks(task_ids)
    summary["total"] = len(tasks)
    for task in tasks:
        tlog = hub.make_logger(run_id, task.id)
        if task.shareurl_ban:
            summary["skipped"] += 1
            tlog("warn", f"《{task.taskname}》已标记失效（{task.shareurl_ban}），跳过")
            continue
        # 全局 sweep（task_ids=None）只驱动「无有效独立调度」的任务：已自带有效 schedule 的任务
        # 由其专属 job 触发，避免同时被主 crontab 双驱动（如"仅周日"cron 却在每日全局点被执行）。
        # schedule 为空或非法的任务仍留在 sweep 中，继承全局回退。
        if (
            trigger == "scheduled"
            and task_ids is None
            and has_valid_schedule(getattr(task, "schedule", "") or "")
        ):
            summary["skipped"] += 1
            tlog("info", f"《{task.taskname}》已配置独立调度（schedule={task.schedule}），全局 sweep 不再驱动")
            continue
        if trigger == "scheduled" and not task_due_today(task):
            summary["skipped"] += 1
            tlog("info", f"《{task.taskname}》按 runweek/enddate 今日不运行")
            continue

        cls = route_driver(task.shareurl)
        if cls is None:
            summary["failed"] += 1
            notify_lines.append(f"❌《{task.taskname}》：没有支持该链接的网盘驱动")
            continue
        if not cls.supported:
            summary["skipped"] += 1
            tlog("info", f"《{task.taskname}》{cls.name} 驱动即将支持，跳过")
            continue

        account = _pick_account(task.account_id, cls.key)
        if account is None:
            summary["failed"] += 1
            notify_lines.append(f"❌《{task.taskname}》：未配置可用的{cls.name}账号")
            tlog("error", f"《{task.taskname}》缺少账号")
            continue

        if account.id not in drivers:
            drivers[account.id] = cls(cookie=account.cookie, proxy=PROXY, index=account.sort_order)
        driver = drivers[account.id]  # type: ignore[assignment]

        tlog("info", f"《{task.taskname}》开始运行")
        # 转存段（engine save + DB 落库）加全局锁串行化：跨并发 run_tasks 不重叠，避免同账号并发转存与 SQLite 写冲突
        async with _run_lock:
            result = await run_update_task(driver, _task_spec(task), magic_regex=magic_regex, log=tlog)
            with session_scope() as session:
                row = session.get(Task, task.id)
                if row:
                    row.last_run_at = datetime.now()
                    if result.status == "banned":
                        row.shareurl_ban = result.message
                    session.add(row)

        icon = STATUS_ICONS.get(result.status)
        if result.status == "updated":
            summary["updated"] += 1
            notify_lines.append(f"✅《{task.taskname}》添加追更：\n{result.render()}")
            tlog("info", f"《{task.taskname}》新增 {len(result.files)} 项")
            if getattr(task, "auto_download", False):
                # 下载在锁外：本地/aria2 可与其它任务的转存并行，不占用转存串行段
                await _download_for_task(driver, task, result, settings, notify_lines, tlog, account_id=account.id)
        elif result.status == "no_changes":
            tlog("info", f"《{task.taskname}》没有新的转存")
        else:
            summary["failed"] += 1
            notify_lines.append(f"{icon}《{task.taskname}》：{result.message}")
            tlog("error", f"《{task.taskname}》{result.status}：{result.message}")

    if notify_lines:
        await _push("小盘自动转存运行结果", "\n".join(notify_lines), push_config, settings, log)


async def _download_for_task(driver, task, result, settings, notify_lines, tlog, *, account_id=None) -> None:
    from .download_service import DownloadSettings, download_task_files

    if not driver.has("download"):
        tlog("warn", f"《{task.taskname}》{driver.name} 驱动不支持下载，跳过")
        return
    cfg = DownloadSettings.from_dict(settings.get("download"))
    try:
        lines = await download_task_files(
            driver,
            result.files,
            cfg,
            download_subdir=bool(getattr(task, "download_subdir", False)),
            savepath_override=getattr(task, "download_savepath", "") or "",
            log=tlog,
            task_id=task.id,
            taskname=task.taskname,
            account_id=account_id,
        )
    except Exception as exc:  # noqa: BLE001 下载失败不影响转存结果
        tlog("error", f"《{task.taskname}》下载异常：{exc}")
        lines = [f"❌ 下载异常: {exc}"]
    ok = sum(1 for line in lines if line.startswith("✅"))
    if lines:
        notify_lines.append(f"📥《{task.taskname}》本地下载 {ok}/{len(lines)}：\n" + "\n".join(lines))


async def _push(title: str, content: str, push_config: dict, settings: dict, log) -> None:
    if not settings.get("notify_enabled", True):
        return
    from .notify_service import push_all  # 延迟导入，模块由并行任务交付

    try:
        for channel, ok, message in await push_all(title, content, push_config, log=log):
            if not ok:
                log("warn", f"通知渠道 {channel} 失败：{message}")
    except Exception as exc:  # noqa: BLE001
        log("error", f"通知发送异常：{exc}")
