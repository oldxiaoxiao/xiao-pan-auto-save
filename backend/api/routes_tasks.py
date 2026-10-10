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


def _to_out(task: Task, health: dict | None = None) -> TaskOut:
    data = task.model_dump()
    data["runweek"] = task.runweek_list()
    data["last_run_at"] = task.last_run_at.isoformat() if task.last_run_at else None
    data["next_retry_at"] = task.next_retry_at.isoformat() if task.next_retry_at else None
    # 老库升级出来的空串在这层归一化：前端与油猴列表永远看不到 ''
    data["run_mode"] = run_mode_of(task)
    if health is None:  # 单个任务（创建/更新）不走批量 map，就地算一次
        try:
            from ..services.task_health import compute_health

            health = compute_health(task)
        except Exception:  # noqa: BLE001
            health = {}
    data["health"] = health
    return TaskOut(**data)


def _health_map() -> dict[int, dict]:
    from ..services.task_health import health_map

    try:
        return health_map()
    except Exception:  # noqa: BLE001 健康度是附加信息，算不出来也不能让列表打不开
        return {}


def _check_run_mode(value: str) -> None:
    """执行方式非法必须报错，不能像 schedule 那样静默回退成每天跑。"""
    if value not in RUN_MODES:
        raise HTTPException(400, f"执行方式只能是 {' / '.join(RUN_MODES)}")


@router.get("", response_model=list[TaskOut])
async def list_tasks() -> list[TaskOut]:
    health = _health_map()
    with session_scope() as session:
        tasks = session.exec(select(Task).order_by(Task.sort_order, Task.id)).all()
        return [_to_out(t, health.get(int(t.id or 0))) for t in tasks]


@router.get("/issues")
async def task_issues() -> dict:
    """FR-03：待处理聚合视图——所有非正常任务连原因一起给，不用去翻日志。"""
    from ..services.task_health import issues

    return {"ok": True, "data": issues()}


@router.get("/{task_id}/runs")
async def task_runs(task_id: int) -> dict:
    """最近几次运行结论，用于失败归因。"""
    from ..services.task_health import recent_runs

    return {"ok": True, "data": recent_runs(task_id)}


@router.post("/{task_id}/revalidate")
async def revalidate(task_id: int) -> dict:
    """重新验证失效的分享链接：真恢复了才清除失效标记，没恢复就如实说。

    不做"看着像就行"的乐观清除——那会让任务在下次运行时再次静默失败。
    """
    from ..core.router import route_driver
    from ..drivers.base import DriveError, ShareBanned, ShareUnavailable
    from ..services.task_health import record_run

    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            raise HTTPException(404, "任务不存在")
        url = task.shareurl
    cls = route_driver(url)
    if cls is None:
        return {"ok": False, "message": "无法识别的网盘链接"}
    if not cls.supported:
        return {"ok": False, "message": f"{cls.name} 驱动即将支持", "pending": True}

    from ..api.routes_files import _driver

    drv = _driver(cls.key)
    try:
        ref = drv.parse_share(url)
        items = await drv.list_share(ref, "")
    except ShareBanned as exc:
        return {"ok": False, "message": exc.message}
    except ShareUnavailable as exc:
        return {"ok": False, "message": f"网络异常：{exc.message}", "retry": True}
    except DriveError as exc:
        return {"ok": False, "message": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}
    finally:
        await drv.close()

    if not items:
        return {"ok": False, "message": "链接可访问但内容为空，请确认分享是否正常"}

    with session_scope() as session:
        row = session.get(Task, task_id)
        if row is not None:
            row.shareurl_ban = ""
            session.add(row)
    record_run(task_id, "revalidated", f"链接已恢复，可访问 {len(items)} 项")
    return {"ok": True, "message": f"链接已恢复（{len(items)} 项），失效标记已清除"}


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
    from ..services.credential_store import plain_cookie

    driver = cls(cookie=plain_cookie(account) or "", proxy=PROXY, index=account.sort_order)
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
        # FR-10：命名正则没匹配上的样本。为空却 0 新增 = 真的没更新；有样本 = 正则写错了
        "unmatched_samples": result.unmatched_samples,
        "items": [
            {"share_name": f.share_name, "final_name": f.final_name, "dest_path": f.dest_path, "is_dir": f.is_dir}
            for f in result.files[:20]
        ],
    }
