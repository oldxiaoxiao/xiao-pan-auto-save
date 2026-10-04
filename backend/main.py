"""FastAPI 应用入口：路由挂载 + 数据库/调度器生命周期。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlmodel import select

from . import __version__, config
from .api import (
    routes_accounts,
    routes_downloads,
    routes_external,
    routes_files,
    routes_logs,
    routes_migrate,
    routes_search,
    routes_settings,
    routes_tasks,
    routes_tokens,
)
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
    from .models import Task
    from .services import task_service

    with session_scope() as s:
        row = s.get(Task, task_id)
    if row is None or row.disabled:
        scheduler.unschedule_task(task_id)  # 任务已删/停用 → 撤销自身定时器
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


def apply_task_schedule(task) -> None:
    from functools import partial

    from .database import session_scope
    from .models import Task

    with session_scope() as s:
        row = s.get(Task, task.id)
    if row is None or row.disabled:
        scheduler.unschedule_task(task.id)
        return
    scheduler.reschedule_task(task.id, getattr(row, "schedule", "") or "", partial(_run_one_task, task.id))


def reschedule_all_tasks() -> None:
    from .database import session_scope
    from .models import Task

    with session_scope() as s:
        tasks = s.exec(select(Task)).all()
    for t in tasks:
        apply_task_schedule(t)


@asynccontextmanager
async def lifespan(app: FastAPI):
    backup_db(config.DB_PATH, config.DATA_DIR / "backups")
    init_db()
    scheduler.start()
    # 启动补一次清理（停机跨过凌晨四点的场景），再挂每日维护 job；replace_existing 保证重启不叠加
    await _prune_job()
    scheduler.add_daily("xiao_pan_prune", _prune_job)
    reschedule_main_job()
    reschedule_all_tasks()
    yield
    scheduler.shutdown()


app = FastAPI(title="xiao-pan-auto-save", version=__version__, lifespan=lifespan)

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


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__, "data_dir": str(config.DATA_DIR)}


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
        file = _DIST / full_path
        if full_path and file.is_file():
            return FileResponse(file)
        # index.html 不缓存：前端重新构建后用户无需强刷即可拿到新 hash 资源
        return FileResponse(_DIST / "index.html", headers={"cache-control": "no-cache"})
