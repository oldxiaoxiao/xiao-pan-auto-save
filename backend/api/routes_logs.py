"""日志：历史查询 + 全局 SSE 流。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter
from starlette.responses import StreamingResponse

from ..core.logstream import hub

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def recent(limit: int = 200) -> list[dict]:
    return list(hub.history)[-limit:]


@router.get("/stream")
async def stream() -> StreamingResponse:
    """订阅所有运行日志（前端日志弹窗用）。"""

    async def event_stream():
        queue = hub.subscribe()
        try:
            for entry in list(hub.history)[-50:]:
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=30)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
