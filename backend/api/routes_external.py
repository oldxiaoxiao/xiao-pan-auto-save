"""对外 API（兼容原项目油猴脚本格式）：/api/add_task + /api/v1/task/*。

鉴权：ExternalApiToken 表 + 环境变量 API_TOKEN；token 从 ?token= 查询串
或 Authorization: Bearer 头取。无任何 token 配置时对外 API 一律 401。
"""

from __future__ import annotations

import os
import secrets

from fastapi import APIRouter, HTTPException, Request
from sqlmodel import select

from ..database import session_scope
from ..models import ExternalApiToken, Task

router = APIRouter(tags=["external"])


def require_token(request: Request) -> ExternalApiToken | None:
    token = request.query_params.get("token") or ""
    if not token:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "缺少 token")
    env_token = os.getenv("API_TOKEN", "")
    if env_token and secrets.compare_digest(token, env_token):
        return None
    with session_scope() as session:
        row = session.get(ExternalApiToken, token)
        if row is None:
            raise HTTPException(401, "token 无效")
        return row


def _old_format(t: Task) -> dict:
    """任务 → 原项目 tasklist 条目格式（插件 addition 不支持，返回空 dict）。"""
    return {
        "id": t.id,
        "taskname": t.taskname,
        "shareurl": t.shareurl,
        "savepath": t.savepath,
        "pattern": t.pattern,
        "replace": t.replace,
        "ignore_extension": t.ignore_extension,
        "startfid": t.startfid,
        "update_subdir": t.update_subdir,
        "update_subdir_resave_mode": t.update_subdir_resave,
        "enddate": t.enddate,
        "runweek": t.runweek_list(),
        "disabled": t.disabled,
        "shareurl_ban": t.shareurl_ban,
        "addition": {},
    }


_ALLOWED_FIELDS = {
    "taskname",
    "shareurl",
    "savepath",
    "pattern",
    "replace",
    "ignore_extension",
    "startfid",
    "update_subdir",
    "enddate",
    "disabled",
}


def _apply_fields(task: Task, data: dict) -> None:
    for key in _ALLOWED_FIELDS:
        if key in data and data[key] is not None:
            setattr(task, key, data[key])
    if "update_subdir_resave_mode" in data:
        task.update_subdir_resave = bool(data["update_subdir_resave_mode"])
    if "runweek" in data and isinstance(data["runweek"], list):
        import json as _json

        task.runweek = _json.dumps([int(x) for x in data["runweek"]])


@router.post("/api/add_task")
@router.post("/api/v1/task/add")
async def add_task(request: Request) -> dict:
    require_token(request)
    data = await request.json()
    for field in ("taskname", "shareurl", "savepath"):
        if not data.get(field):
            return {"success": False, "code": 2, "message": f"缺少必要字段: {field}"}  # 兼容原码
    with session_scope() as session:
        task = Task(
            taskname=data["taskname"],
            shareurl=data["shareurl"],
            savepath=data["savepath"],
            pattern=data.get("pattern", ""),
            replace=data.get("replace", ""),
            ignore_extension=bool(data.get("ignore_extension")),
            startfid=data.get("startfid", ""),
            update_subdir=data.get("update_subdir", ""),
            update_subdir_resave=bool(data.get("update_subdir_resave_mode")),
            enddate=data.get("enddate", ""),
            runweek=str(data.get("runweek", "[]")),
        )
        session.add(task)
        session.commit()
        session.refresh(task)
        payload = _old_format(task)
    return {"success": True, "code": 0, "message": "任务添加成功", "data": payload}


@router.get("/api/v1/task/list")
@router.get("/api/tasklist")
async def list_tasks(request: Request) -> dict:
    require_token(request)
    with session_scope() as session:
        tasks = session.exec(select(Task).order_by(Task.sort_order, Task.id)).all()
        data = [_old_format(t) for t in tasks]
    return {"success": True, "code": 0, "data": data}


@router.post("/api/v1/task/update")
async def update_task(request: Request) -> dict:
    require_token(request)
    data = await request.json()
    task_id = data.get("id")
    name = data.get("taskname")
    with session_scope() as session:
        task = None
        if task_id:
            task = session.get(Task, int(task_id))
        elif name:
            task = session.exec(select(Task).where(Task.taskname == name)).first()
        if task is None:
            return {"success": False, "code": 2, "message": "任务不存在"}
        _apply_fields(task, data)
        session.add(task)
        session.commit()
        session.refresh(task)
        payload = _old_format(task)
    return {"success": True, "code": 0, "message": "任务更新成功", "data": payload}


@router.post("/api/v1/task/run")
async def run_task(request: Request) -> dict:
    require_token(request)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 允许空 body
        data = {}
    from ..services.task_service import run_tasks

    task_id = data.get("id")
    name = data.get("taskname")
    ids: list[int] | None = None
    if task_id or name:
        with session_scope() as session:
            if task_id:
                ids = [int(task_id)]
            else:
                row = session.exec(select(Task).where(Task.taskname == name)).first()
                if row is None:
                    return {"success": False, "code": 2, "message": "任务不存在"}
                ids = [row.id]
    summary = await run_tasks(task_ids=ids, trigger="manual")
    return {"success": True, "code": 0, "message": "运行完成", "data": summary}
