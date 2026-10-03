"""下载进度快照接口（只读）。"""

from __future__ import annotations

from fastapi import APIRouter

from ..core.download_registry import registry

router = APIRouter(prefix="/api", tags=["downloads"])


@router.get("/downloads")
async def downloads() -> dict:
    from ..api.deps import get_setting
    from ..services.download_service import DownloadSettings, aria2_status

    jobs = registry.snapshot()
    jobs += await aria2_status(DownloadSettings.from_dict(get_setting("download")))
    return {"jobs": jobs}
