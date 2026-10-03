"""下载进度快照接口（只读）。"""

from __future__ import annotations

from fastapi import APIRouter

from ..core.download_registry import registry

router = APIRouter(prefix="/api", tags=["downloads"])


@router.get("/downloads")
async def downloads() -> dict:
    return {"jobs": registry.snapshot()}
