"""FastAPI 应用入口：路由挂载 + 数据库/调度器生命周期。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import config
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.start()
    reschedule_main_job()
    yield
    scheduler.shutdown()


app = FastAPI(title="xiao-pan-auto-save", version="0.2.0", lifespan=lifespan)

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
    return {"status": "ok", "data_dir": str(config.DATA_DIR)}


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
