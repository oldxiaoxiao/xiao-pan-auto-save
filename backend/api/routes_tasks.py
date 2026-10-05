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
from ..models import RUN_MODES, Task
from ..schemas import TaskIn, TaskOut
from ..services.task_service import above_sort_order, below_sort_order, run_tasks

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _to_out(task: Task) -> TaskOut:
    data = task.model_dump()
    data["runweek"] = task.runweek_list()
    data["last_run_at"] = task.last_run_at.isoformat() if task.last_run_at else None
    return TaskOut(**data)


def _check_run_mode(value: str) -> None:
    """执行方式非法必须报错，不能像 schedule 那样静默回退成每天跑。"""
    if value not in RUN_MODES:
        raise HTTPException(400, f"执行方式只能是 {' / '.join(RUN_MODES)}")


@router.get("", response_model=list[TaskOut])
async def list_tasks() -> list[TaskOut]:
    with session_scope() as session:
        tasks = session.exec(select(Task).order_by(Task.sort_order, Task.id)).all()
        return [_to_out(t) for t in tasks]


@router.post("", response_model=TaskOut)
async def create_task(body: TaskIn) -> TaskOut:
    _check_run_mode(body.run_mode)
    with session_scope() as session:
        data = body.model_dump(exclude={"runweek"})
        # 新建置顶：请求里的 sort_order 不作数（表单恒发 0，会与当前首行相撞），
        # 一律排在现列表最前，位置不再依赖拖拽历史。
        data["sort_order"] = above_sort_order(session)
        task = Task(**data, runweek=json.dumps(body.runweek))
        session.add(task)
        session.commit()
        session.refresh(task)
        out = _to_out(task)
    from ..main import apply_task_schedule

    apply_task_schedule(task)
    return out


@router.put("/{task_id}", response_model=TaskOut)
async def update_task(task_id: int, body: TaskIn) -> TaskOut:
    _check_run_mode(body.run_mode)
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


@router.post("/{task_id}/position")
async def move_task_position(task_id: int, where: str = "top") -> dict:
    """显式置顶/置底：只改 sort_order。

    不调 apply_task_schedule——它只在 disabled/schedule/enddate 变化时才需要重排作业，
    而这里三样都没动；重复注册反而会把 interval 作业的下次触发时间重置。
    where 先校验再查行（非法参数报 400 而不是被 404 盖住），顺序有用例钉住。
    """
    if where not in ("top", "bottom"):
        raise HTTPException(400, "where 只能是 top 或 bottom")
    with session_scope() as session:
        task = session.get(Task, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        task.sort_order = above_sort_order(session) if where == "top" else below_sort_order(session)
        session.add(task)
        session.commit()
        session.refresh(task)
        return {"ok": True, "sort_order": task.sort_order}


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
