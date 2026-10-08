"""旧配置迁移端点：预览 + 导入。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import migrate_service

router = APIRouter(prefix="/api/migrate", tags=["migrate"])


class MigrateIn(BaseModel):
    config: dict
    overwrite: bool = False


@router.post("/preview")
async def preview(body: MigrateIn) -> dict:
    try:
        return migrate_service.analyze(body.config)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("")
async def migrate(body: MigrateIn) -> dict:
    try:
        result = migrate_service.import_config(body.config, overwrite=body.overwrite)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not result.get("ok"):
        raise HTTPException(409, result.get("message", "导入失败"))
    from ..main import reschedule_all_tasks, reschedule_main_job, scheduler

    # 旧任务 ID 可被复用，先撤销旧作业，再依照导入后的数据库重建。
    for job in scheduler.scheduler.get_jobs():
        if job.id.startswith(("xiao_pan_task_", "xiao_pan_retry_")):
            scheduler.scheduler.remove_job(job.id)
    reschedule_main_job()
    reschedule_all_tasks()
    return result
