"""FastAPI 应用入口：路由挂载 + 数据库/调度器生命周期。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlmodel import select

from . import __version__, config
from .api import (
    routes_accounts,
    routes_agent,
    routes_backup,
    routes_downloads,
    routes_external,
    routes_files,
    routes_logs,
    routes_migrate,
    routes_name_templates,
    routes_overview,
    routes_search,
    routes_settings,
    routes_tasks,
    routes_tokens,
)
from .api.auth import WebAuthMiddleware
from .api.auth import router as auth_router
from .core.logstream import hub
from .core.scheduler import TaskScheduler
from .database import init_db
from .drivers import driver_matrix
from .services.backup_service import backup_db

scheduler = TaskScheduler()


async def _main_job() -> None:
    """定时主运行入口（含签到 + 账号检查 + 任务追更）。"""
    from .api.deps import get_setting
    from .services import account_service, task_service

    log = hub.make_logger("scheduled")
    try:
        if get_setting("sign_enabled"):
            await account_service.sign_accounts(log=log)
            await account_service.refresh_accounts(log=log)
        await task_service.run_tasks(trigger="scheduled")
    except Exception as exc:  # noqa: BLE001 调度任务不允许裸崩
        log("error", f"定时运行异常：{exc}")


def reschedule_main_job() -> None:
    from .api.deps import get_setting

    scheduler.reschedule(str(get_setting("crontab") or config.CRONTAB_DEFAULT), _main_job)


async def _prune_job() -> None:
    """下载历史清理（启动跑一次 + 每天凌晨四点），失败只记日志、绝不影响启动与主任务。"""
    from .api.deps import get_setting
    from .services import download_history

    log = hub.make_logger("prune")
    try:
        retention = str((get_setting("download") or {}).get("history_retention") or "days_90")
        removed = download_history.prune("auto", retention)
        if removed:
            log("info", f"下载历史清理：删除 {removed} 条（保留 {retention}）")
    except Exception as exc:  # noqa: BLE001 清理失败不影响主运行
        log("warn", f"下载历史清理失败：{exc}")


async def _run_one_task(task_id: int) -> None:
    from datetime import date, datetime

    from .database import session_scope
    from .models import Task, run_mode_of
    from .services import task_service

    with session_scope() as s:
        row = s.get(Task, task_id)
    if row is None or row.disabled or run_mode_of(row) != "follow":
        scheduler.unschedule_task(task_id)  # 已删 / 停用 / 改成非自动形态 → 撤销自身定时器
        return
    if row.enddate:
        try:
            if date.today() > datetime.strptime(row.enddate, "%Y-%m-%d").date():
                scheduler.unschedule_task(task_id)  # 过截止日期 → 停止高频空跑
                return
        except ValueError:
            pass
    try:
        await task_service.run_tasks(task_ids=[task_id], trigger="scheduled")
    except Exception as exc:  # noqa: BLE001
        hub.make_logger("scheduled")("error", f"任务 {task_id} 运行异常：{exc}")


async def _run_retry_task(task_id: int) -> None:
    """一次性任务的到点重试：先清掉这一格，再走同一套运行与判定。

    必须先清：once_next_driver 见 next_retry_at 非空会答"等重试作业"，
    不清就等于这轮运行自己把自己跳过。
    """
    from datetime import datetime, timedelta
    from functools import partial

    from .database import session_scope
    from .models import Task
    from .services import task_service

    log = hub.make_logger("scheduled")
    try:
        with session_scope() as s:
            row = s.get(Task, task_id)
            if row is None:
                return
            row.next_retry_at = None
            s.add(row)
    except Exception as exc:  # noqa: BLE001 旁路写库：SQLite 无 WAL/busy_timeout，并发写抛锁冲突
        # 抛回调度器 = 这格到点作业被消费移除而库里格子还在 ⇒ 每日扫永远让位，
        # 和过期作业被 misfire 丢弃是同一种孤儿态。宁可让重试作业继续驱动，
        # 也不能让它永远没人跑：按重试节奏重排一格，到点再清再试；
        # replace_existing 保证同一行始终只有一格，不会叠加。
        log("warn", f"任务 {task_id} 清除重试到点时间失败，本轮不运行：{exc}")
        when = datetime.now() + timedelta(minutes=task_service.ONCE_RETRY_DELAY_MINUTES)
        scheduler.reschedule_retry_at(task_id, when, partial(_run_retry_task, task_id))
        return
    try:
        await task_service.run_tasks(task_ids=[task_id], trigger="scheduled")
    except Exception as exc:  # noqa: BLE001
        log("error", f"任务 {task_id} 重试运行异常：{exc}")


def apply_task_schedule(task) -> None:
    from functools import partial

    from .database import session_scope
    from .models import Task, run_mode_of
    from .services.task_service import once_next_driver

    with session_scope() as s:
        row = s.get(Task, task.id)
    if row is None:
        scheduler.unschedule_task(task.id)
        scheduler.unschedule_retry(task.id)
        return
    scheduler.unschedule_task(task.id)  # 形态可能从 follow 改成 once，旧周期作业必须先撤
    driver = once_next_driver(row)
    if row.disabled or run_mode_of(row) == "manual" or driver == "halted":
        scheduler.unschedule_retry(task.id)
        return
    if run_mode_of(row) == "once":
        # 一次性：只有排了到点时间才注册作业；否则交给每日扫"等放出"
        if driver == "retry" and row.next_retry_at is not None:
            scheduler.reschedule_retry_at(row.id, row.next_retry_at, partial(_run_retry_task, row.id))
        else:
            scheduler.unschedule_retry(task.id)
        return
    # follow 落点：行可能刚从 once 改成 follow，遗留的到点重试作业必须一并撤销，
    # 否则它到点仍会触发 _run_retry_task，把刚变成 follow 的行提前跑一次并清掉 next_retry_at。
    scheduler.unschedule_retry(task.id)
    scheduler.reschedule_task(task.id, getattr(row, "schedule", "") or "", partial(_run_one_task, task.id))


def reschedule_all_tasks() -> None:
    from .database import session_scope
    from .models import Task

    with session_scope() as s:
        tasks = s.exec(select(Task)).all()
    for t in tasks:
        apply_task_schedule(t)


async def _account_check_job() -> None:
    """FR-02：每日账号健康检查 + 失效提醒。

    刻意独立于「自动签到」开关：过去账号检查挂在 sign_enabled 下面，
    关掉签到就等于关掉了失效检测，Cookie 过期只能等任务失败才被发现。
    """
    from .services import account_service

    log = hub.make_logger("account-check")
    try:
        await account_service.refresh_accounts(log=log)
        await account_service.alert_invalid_accounts(log=log)
    except Exception as exc:  # noqa: BLE001 调度任务不允许裸崩
        log("error", f"账号健康检查异常：{exc}")


async def _notify_digest_job() -> None:
    """FR-04：免打扰结束后的摘要补发。

    必须独立于"有没有新事件"：半夜攒下 3 条需处理，早上若没有任何任务运行，
    就永远没人去触发补发，攒的事件会一直躺在队里。
    """
    from .api.deps import get_setting
    from .services import notify_center

    log = hub.make_logger("notify-digest")
    try:
        sent = await notify_center.flush_pending(
            settings={
                "notify_enabled": bool(get_setting("notify_enabled")),
                "notify_quiet": get_setting("notify_quiet"),
            },
            push_config=get_setting("push_config") or {},
            log=log,
        )
        if sent:
            log("info", f"免打扰摘要补发 {sent} 条")
    except Exception as exc:  # noqa: BLE001 调度任务不允许裸崩
        log("error", f"免打扰摘要补发异常：{exc}")


def reschedule_notify_digest() -> None:
    """按免打扰结束时间安排摘要作业；免打扰关掉时仍挂一个默认时间点兜底。

    挂作业的用意是"到点一定有人去补发"，而不是精确对时：真正下发前 flush_pending
    还会再判一次窗口，没出窗口就不发。
    """
    from .api.deps import get_setting
    from .services.notify_center import DEFAULT_QUIET, DIGEST_DELAY_MINUTES, quiet_config

    cfg = quiet_config({"notify_quiet": get_setting("notify_quiet")})
    end = cfg["end"] or DEFAULT_QUIET["end"]
    try:
        hour, minute = (int(x) for x in str(end).split(":")[:2])
    except (ValueError, TypeError):
        hour, minute = 8, 0
    minute = min(minute + DIGEST_DELAY_MINUTES, 59)
    scheduler.add_daily("xiao_pan_notify_digest", _notify_digest_job, hour=hour, minute=minute)


@asynccontextmanager
async def lifespan(app: FastAPI):
    backup_db(config.DB_PATH, config.DATA_DIR / "backups")
    init_db()
    # FR-07：先把上次进程留下的内置下载在途行收口成 interrupted。
    # 必须在 scheduler 启动之前——否则新任务可能与这些半成品抢同一个 .part。
    try:
        from .services.download_history import mark_interrupted

        orphan = mark_interrupted()
        if orphan:
            hub.make_logger("startup")("warn", f"上次有 {orphan} 个内置下载被中断，已标记为可继续")
    except Exception as exc:  # noqa: BLE001 启动阶段不允许因收口失败而挂掉
        hub.make_logger("startup")("warn", f"中断下载收口失败（不影响启动）：{exc}")
    scheduler.start()
    # 启动补一次清理（停机跨过凌晨四点的场景），再挂每日维护 job；replace_existing 保证重启不叠加
    # XIAO_PAN_SKIP_STARTUP_PRUNE=1（测试环境）只跳启动这一次，每日 job 照常注册
    if not config.SKIP_STARTUP_PRUNE:
        await _prune_job()
    scheduler.add_daily("xiao_pan_prune", _prune_job)
    scheduler.add_daily("xiao_pan_account_check", _account_check_job)
    reschedule_notify_digest()
    reschedule_main_job()
    reschedule_all_tasks()
    yield
    scheduler.shutdown()


app = FastAPI(title="xiao-pan-auto-save", version=__version__, lifespan=lifespan)
app.add_middleware(WebAuthMiddleware)
app.include_router(auth_router)

app.include_router(routes_tasks.router)
app.include_router(routes_accounts.router)
app.include_router(routes_settings.router)
app.include_router(routes_logs.router)
app.include_router(routes_files.router)
app.include_router(routes_search.router)
app.include_router(routes_external.router)
app.include_router(routes_tokens.router)
app.include_router(routes_migrate.router)
app.include_router(routes_downloads.router)
app.include_router(routes_agent.router)
app.include_router(routes_overview.router)
app.include_router(routes_name_templates.router)
app.include_router(routes_backup.router)


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "data_dir": str(config.DATA_DIR),
        "desktop_mode": config.DESKTOP_MODE,
    }


@app.get("/api/drivers")
async def drivers() -> list[dict]:
    return driver_matrix()


@app.get("/api/scheduler")
async def scheduler_info() -> dict:
    job = scheduler.scheduler.get_job("xiao_pan_main_run")
    return {
        "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "trigger": str(job.trigger) if job else None,
    }


# ---------- 前端静态托管：存在构建产物时启用，SPA 路由回退 index.html ----------
_DIST = config.BASE_DIR / "frontend" / "dist"
if _DIST.is_dir():
    from fastapi.responses import FileResponse
    from starlette.staticfiles import StaticFiles

    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        file = (_DIST / full_path).resolve()
        if not file.is_relative_to(_DIST.resolve()):
            raise HTTPException(404, "文件不存在")
        if full_path and file.is_file():
            return FileResponse(file)
        # index.html 不缓存：前端重新构建后用户无需强刷即可拿到新 hash 资源
        return FileResponse(_DIST / "index.html", headers={"cache-control": "no-cache"})
