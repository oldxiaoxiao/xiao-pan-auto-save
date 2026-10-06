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
from ..models import RUN_MODES, Task, run_mode_of
from ..schemas import TaskIn, TaskOut
from ..services.task_service import above_sort_order, below_sort_order, run_tasks

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _to_out(task: Task) -> TaskOut:
    data = task.model_dump()
    data["runweek"] = task.runweek_list()
    data["last_run_at"] = task.last_run_at.isoformat() if task.last_run_at else None
    data["next_retry_at"] = task.next_retry_at.isoformat() if task.next_retry_at else None
    # 老库升级出来的空串在这层归一化：前端与油猴列表永远看不到 ''
    data["run_mode"] = run_mode_of(task)
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
    """立即运行单个任务，SSE 流式返回日志。

    对一次性任务，这一按就是「手动再次开启」：先归零预算再跑，否则用尽后永远出不来。
    """
    from ..services.task_service import reset_once_budget

    reset_once_budget(task_id)
    return _sse_stream([task_id], "manual")


@router.post("/dry-run")
async def dry_run(body: TaskIn) -> dict:
    """试跑：只读地告诉你这一跑会转什么，判定与真实运行共用 engine._check_dir，不写库、不占运行锁。"""
    from ..core.engine import run_update_task
    from ..core.router import route_driver
    from ..services.task_service import _pick_account

    cls = route_driver(body.shareurl)
    if cls is None or not cls.supported:
        return {"ok": False, "status": "failed", "message": "该链接没有已支持的网盘驱动", "items": []}
    account = _pick_account(body.account_id, cls.key)
    if account is None:
        return {"ok": False, "status": "failed", "message": f"未配置可用的{cls.name}账号", "items": []}

    from ..api.deps import all_settings
    from ..config import PROXY
    from ..services.task_service import _task_spec

    spec = _task_spec(body)  # TaskIn 与 Task 字段同名，直接复用
    driver = cls(cookie=account.cookie, proxy=PROXY, index=account.sort_order)
    try:
        result = await run_update_task(driver, spec, magic_regex=all_settings().get("magic_regex") or {}, plan_only=True)
    finally:
        await driver.close()

    return {
        "ok": result.status not in ("failed", "banned", "network"),
        "status": result.status,
        "message": result.message,
        "new_count": sum(1 for f in result.files if not f.is_dir),
        "total_size": sum(f.size for f in result.files),
        "skipped_existing": result.planned_existing,
        "filtered_out": result.filtered_out,
        "items": [
            {"share_name": f.share_name, "final_name": f.final_name, "dest_path": f.dest_path, "is_dir": f.is_dir}
            for f in result.files[:20]
        ],
    }
