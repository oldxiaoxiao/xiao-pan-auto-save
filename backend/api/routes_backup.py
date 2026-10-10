"""备份导出与恢复（FR-05）：支持不含凭据的脱敏导出，恢复前自动快照当前库。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import __version__, config
from ..services import backup_service

router = APIRouter(prefix="/api/backup", tags=["backup"])


class ImportIn(BaseModel):
    payload: dict


@router.get("/export")
async def export(mode: str = "safe") -> dict:
    """mode=safe（默认）：剔除账号 Cookie；mode=full：包含凭据，仅在本机保管。"""
    if mode not in ("safe", "full"):
        raise HTTPException(400, "mode 只能是 safe 或 full")
    return {"ok": True, "data": backup_service.export_data(mode), "version": __version__}


@router.post("/import")
async def import_backup(body: ImportIn) -> dict:
    result = backup_service.import_data(body.payload, config.DB_PATH, config.DATA_DIR / "backups")
    if not result["ok"]:
        raise HTTPException(400, result["message"])
    return {"ok": True, **result}
