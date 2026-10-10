"""FR-09 总览接口：一屏回答「追更还好吗」。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter

from ..api.deps import get_setting
from ..services import overview_service
from ..services.download_service import DownloadSettings

router = APIRouter(prefix="/api/overview", tags=["overview"])


def _download_dir() -> Path:
    """下载根目录：与账本 dest_path 同源，避免总览看的磁盘和实际落盘的不是一块。"""
    cfg = DownloadSettings.from_dict(get_setting("download"))
    return Path(cfg.dir)


@router.get("")
async def overview() -> dict:
    try:
        data = overview_service.build(_download_dir())
    except Exception as exc:  # noqa: BLE001 总览是观测面，任何一块读不出来都不该让整页 500
        return {
            "ok": False,
            "message": f"总览数据读取失败：{exc}",
            "data": {"level": "unknown", "generated_at": ""},
        }
    return {"ok": True, "data": data}
