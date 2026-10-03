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
    if "tasklist" not in body.config and "cookie" not in body.config:
        raise HTTPException(400, "不像 quark_config.json：缺少 tasklist/cookie 字段")
    return migrate_service.analyze(body.config)


@router.post("")
async def migrate(body: MigrateIn) -> dict:
    result = migrate_service.import_config(body.config, overwrite=body.overwrite)
    if not result.get("ok"):
        raise HTTPException(409, result.get("message", "导入失败"))
    return result
