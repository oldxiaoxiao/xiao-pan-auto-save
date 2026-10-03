"""下载进度快照接口 + 控制端点（stop/pause/resume/delete，按 source 分派 builtin/aria2）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..core.download_registry import registry

router = APIRouter(prefix="/api", tags=["downloads"])


@router.get("/downloads")
async def downloads() -> dict:
    from ..api.deps import get_setting
    from ..services.download_service import DownloadSettings, aria2_status

    jobs = registry.snapshot()
    jobs += await aria2_status(DownloadSettings.from_dict(get_setting("download")))
    return {"jobs": jobs}


@router.post("/downloads/{job_id}/stop")
async def stop(job_id: str, source: str = "builtin") -> dict:
    if source == "aria2":
        return await _aria2_ctl(job_id, "aria2.remove")
    registry.stop(job_id)
    return {"ok": True}


@router.post("/downloads/{job_id}/pause")
async def pause(job_id: str, source: str = "builtin") -> dict:
    if source != "aria2":
        raise HTTPException(400, "内置下载器不支持暂停")
    return await _aria2_ctl(job_id, "aria2.pause")


@router.post("/downloads/{job_id}/resume")
async def resume(job_id: str, source: str = "builtin") -> dict:
    if source != "aria2":
        raise HTTPException(400, "内置下载器不支持继续")
    return await _aria2_ctl(job_id, "aria2.unpause")


@router.delete("/downloads/{job_id}")
async def delete(job_id: str, source: str = "builtin") -> dict:
    if source == "aria2":
        return await _aria2_ctl(job_id, "aria2.removeDownloadResult")
    registry.remove(job_id)
    return {"ok": True}


async def _aria2_ctl(gid: str, method: str) -> dict:
    from ..api.deps import get_setting
    from ..services.download_service import DownloadSettings, aria2_rpc

    cfg = DownloadSettings.from_dict(get_setting("download"))
    try:
        result = await aria2_rpc(cfg, method, gid)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"aria2 不可达：{exc}") from exc
    if result.get("error"):
        raise HTTPException(502, f"aria2 报错：{result['error']}")
    return {"ok": True}
