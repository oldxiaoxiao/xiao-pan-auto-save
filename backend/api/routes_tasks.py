"""任务 CRUD 与运行（SSE 流式日志）。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from sqlmodel import select
from starlette.responses import StreamingResponse

from ..core.logstream import hub
from ..database import session_scope
from ..models import Task
from ..schemas import TaskIn, TaskOut
from ..services.task_service import run_tasks

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _to_out(task: Task) -> TaskOut:
    data = task.model_dump()
    data["runweek"] = task.runweek_list()
    data["last_run_at"] = task.last_run_at.isoformat() if task.last_run_at else None
    return TaskOut(**data)


@router.get("", response_model=list[TaskOut])
async def list_tasks() -> list[TaskOut]:
    with session_scope() as session:
        tasks = session.exec(select(Task).order_by(Task.sort_order, Task.id)).all()
        return [_to_out(t) for t in tasks]


@router.post("", response_model=TaskOut)
async def create_task(body: TaskIn) -> TaskOut:
    with session_scope() as session:
        task = Task(**body.model_dump(exclude={"runweek"}), runweek=json.dumps(body.runweek))
        session.add(task)
        session.commit()
        session.refresh(task)
        out = _to_out(task)
    from ..main import apply_task_schedule

    apply_task_schedule(task)
    return out


@router.put("/{task_id}", response_model=TaskOut)
async def update_task(task_id: int, body: TaskIn) -> TaskOut:
    with session_scope() as session:
        task = session.get(Task, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        for k, v in body.model_dump(exclude={"runweek"}).items():
            setattr(task, k, v)
        task.runweek = json.dumps(body.runweek)
        session.add(task)
        session.commit()
        session.refresh(task)
        out = _to_out(task)
    from ..main import apply_task_schedule

    apply_task_schedule(task)
    return out


@router.delete("/{task_id}")
async def delete_task(task_id: int) -> dict:
    with session_scope() as session:
        task = session.get(Task, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        session.delete(task)
    from ..main import scheduler

    scheduler.unschedule_task(task_id)
    return {"ok": True}


def _sse_stream(task_ids: list[int] | None, trigger: str) -> StreamingResponse:
    async def event_stream():
        queue = hub.subscribe()
        job = asyncio.create_task(run_tasks(task_ids=task_ids, trigger=trigger))
        try:
            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=20)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                if entry.get("level") == "done":
                    break
            try:
                summary = await job
            except Exception as exc:  # noqa: BLE001 运行崩溃也要给前端交代
                summary = {"run_id": "", "error": str(exc)}
            payload = {
                "level": "summary",
                "message": json.dumps(summary, ensure_ascii=False),
                "ts": datetime.now().isoformat(timespec="seconds"),
                "run_id": summary.get("run_id", ""),
                "task_id": None,
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/run")
async def run_all() -> StreamingResponse:
    """立即运行全部任务，SSE 流式返回日志。"""
    return _sse_stream(None, "manual")


@router.post("/{task_id}/run")
async def run_one(task_id: int) -> StreamingResponse:
    """立即运行单个任务，SSE 流式返回日志。"""
    return _sse_stream([task_id], "manual")
