"""下载进度快照接口 + 控制端点（stop/pause/resume/delete，按 source 分派 builtin/aria2）+ 历史账本查询与单文件重下。"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core.download_registry import registry

router = APIRouter(prefix="/api", tags=["downloads"])


@router.get("/downloads")
async def downloads() -> dict:
    from ..api.deps import get_setting
    from ..services.download_service import DownloadSettings, aria2_status

    jobs = registry.snapshot()
    jobs += await aria2_status(DownloadSettings.from_dict(get_setting("download")))
    return {"jobs": jobs}


# 注意：历史相关路由必须声明在 /{job_id} 模式之前，否则
# POST /api/downloads/history/prune 会被 /{job_id}/{action} 抢先匹配、
# DELETE /api/downloads/history/{id} 会把 history 当成 job_id。
@router.get("/downloads/history")
async def history_list(page: int = 1, page_size: int = 50, status: str = "", task_id: int | None = None,
                       keyword: str = "") -> dict:
    from ..api.deps import get_setting
    from ..services import download_history
    from ..services.download_service import DownloadSettings

    cfg = DownloadSettings.from_dict(get_setting("download"))
    await download_history.reconcile(cfg)
    data = await asyncio.to_thread(
        download_history.list_records, page=page, page_size=page_size, status=status, task_id=task_id, keyword=keyword
    )
    states = await asyncio.to_thread(lambda: [download_history.file_state(i["dest_path"]) for i in data["items"]])
    for item, state in zip(data["items"], states, strict=True):
        item["file_state"] = state
    return data


@router.delete("/downloads/history/{record_id}")
async def history_delete(record_id: int) -> dict:
    from ..services import download_history

    if not download_history.delete_record(record_id):
        raise HTTPException(404, "记录不存在")
    return {"ok": True}


@router.post("/downloads/history/{record_id}/retry")
async def history_retry(record_id: int) -> dict:
    """起后台任务重下：内置下载器一个 4K 文件可能跑几小时，绝不同步等待。

    这里只做记录存在性校验；驱动/账号是否可用由 retry_record 在后台任务里判定并写日志。
    """
    from ..api.deps import get_setting
    from ..core.logstream import hub
    from ..services import download_history
    from ..services.download_service import DownloadSettings, retry_record

    rec = download_history.get_record(record_id)
    if rec is None:
        raise HTTPException(404, "记录不存在")
    cfg = DownloadSettings.from_dict(get_setting("download"))
    log = hub.make_logger("retry", task_id=rec.get("task_id"))
    asyncio.create_task(retry_record(rec, cfg, log=log))
    return {"ok": True, "message": "重下已开始，稍后刷新查看"}


class PrunePayload(BaseModel):
    mode: str = "auto"  # auto | failed | all


@router.post("/downloads/history/prune")
async def history_prune(payload: PrunePayload) -> dict:
    """手动清理账本（只删记录不删文件）。路由排在 /{job_id} 之前，否则 history 会被当成 job id。"""
    from ..api.deps import get_setting
    from ..services import download_history

    retention = str((get_setting("download") or {}).get("history_retention") or "days_90")
    removed = download_history.prune(payload.mode, retention)
    return {"ok": True, "removed": removed}


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
