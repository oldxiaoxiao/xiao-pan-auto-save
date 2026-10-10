"""Agent 工具层：对现有 service/路由的薄封装。

设计约束（agent-design.md 5.3）：
- 只读工具可直接调用；写工具必须经确认后才真正执行；
- 不提供删除类工具，不提供任意 SQL / 命令 / 任意 HTTP 能力；
- 返回值不含 Cookie、API Key、本地绝对路径。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from sqlmodel import select

from ..database import session_scope
from ..models import Account, DownloadRecord, Task


class ToolError(RuntimeError):
    """工具执行失败，原因要能回传给模型与用户。"""


class ToolForbidden(RuntimeError):
    """工具不在白名单内（模型幻觉或注入尝试）。"""


class ToolNeedsConfirm(RuntimeError):
    """写工具被调用但尚未确认：转成待确认动作卡。"""

    def __init__(self, name: str, args: dict):
        super().__init__(name)
        self.name = name
        self.args = args


READ_TOOLS = (
    "list_tasks",
    "get_task",
    "list_downloads",
    "list_accounts",
    "search_resources",
    "check_health",
    "browse_share",
    "browse_dir",
    "get_settings_summary",
)
WRITE_TOOLS = ("create_task", "update_task", "run_task", "retry_download")


def _dt(value) -> str:
    return value.isoformat(timespec="seconds") if value else ""


TOOLS: list[dict] = [
    {
        "name": "list_tasks",
        "description": "按名称模糊搜索追更任务，返回摘要（不含密钥与本地路径）。不知道任务 id 时先调它。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "任务名关键词，留空列全部"},
                "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
            },
        },
    },
    {
        "name": "get_task",
        "description": "读取单个任务的完整配置与最近运行状态（不含密钥与本地路径）。",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "list_downloads",
        "description": "查询下载历史账本（状态、文件、目标、时间）。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "按任务筛选，可省略"},
                "status": {"type": "string", "description": "done/failed/downloading/queued/skipped/stopped，可省略"},
                "keyword": {"type": "string", "description": "文件名关键词，可省略"},
                "limit": {"type": "integer", "description": "默认 20"},
            },
        },
    },
    {
        "name": "list_accounts",
        "description": "列出网盘账号的状态摘要（有效性、容量、最近检查时间）。不含任何凭据。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "search_resources",
        "description": "用已配置的公开搜索源检索网盘分享资源，返回标题、来源与链接。链接有效性由搜索源决定。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词，如剧名"},
                "deep": {"type": "boolean", "description": "是否深度搜索，默认否"},
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "browse_share",
        "description": "只读浏览一个网盘分享链接里的内容（不转存、不下载）。",
        "parameters": {
            "type": "object",
            "properties": {
                "shareurl": {"type": "string"},
                "path": {"type": "string", "description": "分享内子路径，可省略"},
            },
            "required": ["shareurl"],
        },
    },
    {
        "name": "check_health",
        "description": (
            "查看任务健康度与失败归因。不传 task_id：列出所有「待处理/停摆」的任务及原因；"
            "传入：返回该任务的状态、原因与最近几次运行记录。"
            "回答「哪些任务有问题」「为什么没跑成功」「最近有什么异常」时直接用它，不要自己从任务列表推断。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "integer", "description": "可选；不传则列出所有异常任务"}},
        },
    },
    {
        "name": "browse_dir",
        "description": "只读浏览自己网盘里的目录内容（不转存、不下载）。统计「已有多少集/最新一集」时用它读任务的保存目录。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "网盘目录路径，如 /来自：分享/剧名"},
                "driver": {"type": "string", "description": "网盘驱动，默认 quark"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_settings_summary",
        "description": "读取全局设置摘要（调度、下载模式、通知开关、已启用搜索源）。不含密钥与本地路径。",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "create_task",
        "description": "创建一个追更任务。需要用户确认后才会真正创建。",
        "parameters": {
            "type": "object",
            "properties": {
                "taskname": {"type": "string"},
                "shareurl": {"type": "string"},
                "savepath": {"type": "string", "description": "网盘保存目录"},
                "quality": {"type": "string", "description": "画质，如 4k/1080p，可省略"},
                "pattern": {"type": "string", "description": "正则筛选，可省略"},
                "replace": {"type": "string", "description": "正则替换，可省略"},
                "auto_download": {"type": "boolean", "description": "转存后是否下载到本地"},
                "episode_start": {"type": "integer", "description": "起始集（含），只转存这一集时起止填同一个数；0=不限"},
                "episode_end": {"type": "integer", "description": "结束集（含），0=不限"},
                "run_mode": {
                    "type": "string",
                    "description": "follow=建追更任务自动跟新集（默认）| once=只追这一次 | manual=只建不自动跑",
                },
                "run_after_create": {"type": "boolean", "description": "创建成功后立即运行一次（下载类请求默认 true）"},
            },
            "required": ["taskname", "shareurl", "savepath"],
        },
    },
    {
        "name": "update_task",
        "description": "修改已有任务的字段（只改传入的字段）。需要用户确认后才会真正修改。",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "taskname": {"type": "string"},
                "savepath": {"type": "string"},
                "quality": {"type": "string"},
                "auto_download": {"type": "boolean"},
                "disabled": {"type": "boolean"},
                "run_mode": {"type": "string", "description": "follow=定时追更 / manual=仅手动 / once=一次性"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "run_task",
        "description": "立即运行一个任务（转存并按配置下载）。需要用户确认后才会真正运行。",
        "parameters": {
            "type": "object",
            "properties": {"task_id": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "retry_download",
        "description": "重新下载一条失败的下载记录（会重新取直链，沿用原来的目标路径）。需要用户确认后才会真正开始。",
        "parameters": {
            "type": "object",
            "properties": {"record_id": {"type": "integer", "description": "下载历史记录 id"}},
            "required": ["record_id"],
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}
TOOL_META = {t["name"]: t for t in TOOLS}


# ------------------------------------------------------------------ 只读实现


def _task_row(t: Task) -> dict:
    return {
        "id": t.id,
        "taskname": t.taskname,
        "savepath": t.savepath,
        "quality": t.quality,
        "run_mode": t.run_mode,
        "disabled": t.disabled,
        "shareurl_ban": t.shareurl_ban,
        "last_run_at": _dt(t.last_run_at),
        "auto_download": t.auto_download,
        "episode_start": t.episode_start,
        "episode_end": t.episode_end,
    }


async def _list_tasks(args: dict) -> dict:
    keyword = (args.get("keyword") or "").strip()
    limit = min(int(args.get("limit") or 20), 50)
    with session_scope() as session:
        tasks = session.exec(select(Task).order_by(Task.sort_order, Task.id)).all()
    rows = [_task_row(t) for t in tasks]
    if keyword:
        rows = [r for r in rows if keyword.lower() in (r["taskname"] or "").lower()]
    return {
        "ok": True,
        "summary": f"共 {len(rows)} 个任务" + (f"（关键词「{keyword}」）" if keyword else ""),
        "total": len(rows),
        "data": rows[:limit],
        "sources": [{"type": "tasks", "ref": "/tasks", "as_of": _dt(datetime.now())}],
    }


async def _get_task(args: dict) -> dict:
    task_id = int(args.get("task_id") or 0)
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return {"ok": False, "summary": f"任务 {task_id} 不存在", "data": {}, "sources": []}
        row = _task_row(task)
        row["shareurl"] = task.shareurl
        row["pattern"] = task.pattern
        row["replace"] = task.replace
        row["schedule"] = task.schedule
        row["update_subdir"] = task.update_subdir
        row["account_id"] = task.account_id
        records = session.exec(
            select(DownloadRecord).where(DownloadRecord.task_id == task_id).order_by(DownloadRecord.id.desc()).limit(5)
        ).all()
    row["recent_downloads"] = [
        {"filename": r.filename, "status": r.status, "finished_at": _dt(r.finished_at)} for r in records
    ]
    return {
        "ok": True,
        "summary": f"任务「{row['taskname']}」" + ("已失效" if row["shareurl_ban"] else ""),
        "data": row,
        "sources": [{"type": "task", "ref": f"/tasks?task_id={task_id}", "as_of": _dt(datetime.now())}],
    }


async def _list_downloads(args: dict) -> dict:
    limit = min(int(args.get("limit") or 20), 50)
    task_id = args.get("task_id")
    status = (args.get("status") or "").strip()
    keyword = (args.get("keyword") or "").strip().lower()
    with session_scope() as session:
        rows = session.exec(select(DownloadRecord).order_by(DownloadRecord.id.desc()).limit(300)).all()
    out = []
    for r in rows:
        if task_id is not None and r.task_id != int(task_id):
            continue
        if status and r.status != status:
            continue
        if keyword and keyword not in (r.filename or "").lower():
            continue
        out.append(
            {
                "id": r.id,
                "task_id": r.task_id,
                "taskname": r.taskname,
                "filename": r.filename,
                "status": r.status,
                "size_total": r.size_total,
                "error": r.error,
                "created_at": _dt(r.created_at),
            }
        )
        if len(out) >= limit:
            break
    return {
        "ok": True,
        "summary": f"命中 {len(out)} 条下载记录",
        "data": out,
        "sources": [{"type": "downloads", "ref": "/downloads", "as_of": _dt(datetime.now())}],
    }


async def _list_accounts(args: dict) -> dict:
    with session_scope() as session:
        rows = session.exec(select(Account).order_by(Account.sort_order, Account.id)).all()
    data = [
        {
            "id": a.id,
            "name": a.name,
            "driver_key": a.driver_key,
            "enabled": a.enabled,
            "can_save": a.can_save,
            "capacity_used": a.capacity_used,
            "capacity_total": a.capacity_total,
            "last_check_at": _dt(a.last_check_at),
        }
        for a in rows
    ]
    return {
        "ok": True,
        "summary": f"共 {len(data)} 个账号（不含任何凭据）",
        "data": data,
        "sources": [{"type": "accounts", "ref": "/accounts", "as_of": _dt(datetime.now())}],
    }


async def _search_resources(args: dict) -> dict:
    keyword = (args.get("keyword") or "").strip()
    if not keyword:
        return {"ok": False, "summary": "缺少搜索关键词", "data": [], "sources": []}
    from ..api.deps import get_setting
    from ..services.search_engines import normalize_source_cfg
    from ..services.search_service import search_all

    engines = normalize_source_cfg(get_setting("source"))
    result = await search_all(keyword, bool(args.get("deep")), {"engines": engines})
    data = []
    for row in result.get("data", [])[:15]:
        # 搜索服务三个适配器统一返回 taskname/content/datetime/channel，
        # 这里两个命名都兜一下，避免以后改字段名导致助手拿到空标题。
        data.append(
            {
                "title": (row.get("taskname") or row.get("title") or "").strip(),
                "shareurl": row.get("shareurl", ""),
                "source": row.get("source") or row.get("channel") or row.get("engine") or "",
                "time": row.get("datetime") or row.get("time") or "",
                "content": (row.get("content") or "").strip()[:160],
            }
        )
    errors = [{"engine": e.get("engine", ""), "message": e.get("message", "")} for e in result.get("errors", [])]
    return {
        "ok": bool(data),
        "summary": f"搜到 {len(data)} 条资源" + (f"；{len(errors)} 个源未出结果" if errors else ""),
        "data": data,
        "errors": errors,
        "sources": [
            {"type": "search", "ref": f"公开搜索源：{keyword}", "as_of": _dt(datetime.now())},
        ],
    }


async def _browse_share(args: dict) -> dict:
    from fastapi import HTTPException

    from ..api.routes_files import SharePreviewIn, share_preview

    try:
        res = await share_preview(
            SharePreviewIn(shareurl=args.get("shareurl", ""), path=args.get("path", "") or "")
        )
    except HTTPException as exc:
        return {"ok": False, "summary": f"读取分享失败：{exc.detail}", "data": [], "sources": []}
    except Exception as exc:  # noqa: BLE001 工具失败要回传原因，不能让整轮崩掉
        return {"ok": False, "summary": f"读取分享失败：{exc}", "data": [], "sources": []}
    items = [
        {"name": i.get("name", ""), "is_dir": i.get("is_dir", False), "size": i.get("size", 0)}
        for i in res.get("list", [])[:20]
    ]
    return {
        "ok": res.get("ok", False),
        "summary": res.get("message") or f"分享内共 {len(items)} 项",
        "data": items,
        "sources": [{"type": "share", "ref": "网盘分享（只读浏览）", "as_of": _dt(datetime.now())}],
    }


async def _check_health(args: dict) -> dict:
    from ..services.task_health import compute_health, issues, recent_runs

    labels = {"ok": "正常", "attention": "待处理", "stale": "停摆"}
    task_id = int(args.get("task_id") or 0)
    source = {"type": "health", "ref": "/tasks", "as_of": _dt(datetime.now())}

    if task_id:
        with session_scope() as session:
            task = session.get(Task, task_id)
        if task is None:
            return {"ok": False, "summary": f"任务 {task_id} 不存在", "data": {}, "sources": []}
        health = compute_health(task)
        runs = recent_runs(task_id, 5)
        label = labels.get(health["status"], health["status"])
        summary = f"任务「{task.taskname}」：{label}" + (f"，{health['reason']}" if health["reason"] else "")
        if health["fail_streak"]:
            summary += f"（连续失败 {health['fail_streak']} 次）"
        return {
            "ok": True,
            "summary": summary,
            "data": {"taskname": task.taskname, "health": health, "recent_runs": runs},
            "sources": [source],
        }

    rows = issues()
    with session_scope() as session:
        total = len(session.exec(select(Task)).all())
    return {
        "ok": True,
        "summary": f"共 {total} 个任务，其中 {len(rows)} 个需要处理" if rows else f"共 {total} 个任务，暂无异常",
        "data": rows,
        "total": total,
        "sources": [source],
    }


async def _browse_dir(args: dict) -> dict:
    from fastapi import HTTPException

    from ..api.routes_files import list_dir

    try:
        res = await list_dir(path=args.get("path") or "/", driver=args.get("driver") or "quark")
    except HTTPException as exc:
        return {"ok": False, "summary": f"读取目录失败：{exc.detail}", "data": [], "sources": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "summary": f"读取目录失败：{exc}", "data": [], "sources": []}
    items = [
        {"name": i.get("name", ""), "is_dir": i.get("is_dir", False), "size": i.get("size", 0)}
        for i in res.get("list", [])[:50]
    ]
    files = [i for i in items if not i["is_dir"]]
    return {
        "ok": True,
        "summary": f"目录 {res.get('path', '')} 共 {len(items)} 项（其中文件 {len(files)} 个）",
        "count": len(items),
        "file_count": len(files),
        "data": items,
        "sources": [{"type": "dir", "ref": "网盘目录（只读浏览）", "as_of": _dt(datetime.now())}],
    }


async def _settings_summary(args: dict) -> dict:
    from ..api.deps import all_settings
    from ..config import DESKTOP_MODE

    st = all_settings()
    dl = st.get("download") or {}
    engines = (st.get("source") or {}).get("engines") or []
    data = {
        "crontab": st.get("crontab", ""),
        "download_mode": dl.get("mode", ""),
        "download_dir_configured": bool(dl.get("dir")),
        "notify_enabled": st.get("notify_enabled", False),
        "sign_enabled": st.get("sign_enabled", False),
        # 部署形态影响"下载到本地"落在哪台机器：本机客户端 vs 服务器/NAS 容器
        "deploy_mode": "desktop" if DESKTOP_MODE else "compose",
        "search_engines": [{"name": e.get("name", ""), "enable": e.get("enable", False)} for e in engines],
    }
    return {
        "ok": True,
        "summary": f"调度 {data['crontab']}；下载模式 {data['download_mode']}；部署形态 {data['deploy_mode']}",
        "data": data,
        "sources": [{"type": "settings", "ref": "/settings", "as_of": _dt(datetime.now())}],
    }


# ------------------------------------------------------------------ 写实现


async def _create_task(args: dict) -> dict:
    from ..api.deps import get_setting
    from ..api.routes_tasks import create_task
    from ..schemas import TaskIn

    taskname = args.get("taskname", "")
    savepath = (args.get("savepath") or "").strip()
    if not savepath:
        # 系统兜底：与新建表单同源的「保存路径根 + 剧名」，省得助手拿不到默认就去问用户
        root = str((get_setting("task_defaults") or {}).get("savepath_root") or "/来自：分享").strip("/")
        savepath = f"/{root}/{taskname}".replace("//", "/")

    body = TaskIn(
        taskname=taskname,
        shareurl=args.get("shareurl", ""),
        savepath=savepath,
        quality=args.get("quality", "") or "",
        pattern=args.get("pattern", "") or "",
        replace=args.get("replace", "") or "",
        auto_download=bool(args.get("auto_download", True)),
        episode_start=int(args.get("episode_start") or 0),
        episode_end=int(args.get("episode_end") or 0),
        run_mode=args.get("run_mode") or "follow",
    )
    out = await create_task(body)

    extra = ""
    if args.get("run_after_create"):
        from ..services.task_service import run_tasks

        asyncio.create_task(run_tasks(task_ids=[out.id], trigger="agent"))
        extra = "，已在后台开始运行"

    scope = ""
    if body.episode_start or body.episode_end:
        scope = f"（只取第 {body.episode_start or '?'}~{body.episode_end or '?'} 集）"
    return {
        "ok": True,
        "summary": f"已创建任务「{out.taskname}」（id={out.id}）{scope}，保存目录 {out.savepath}{extra}",
        "data": {"id": out.id, "taskname": out.taskname, "savepath": out.savepath},
        "sources": [{"type": "task", "ref": f"/tasks?task_id={out.id}", "as_of": _dt(datetime.now())}],
    }


async def _update_task(args: dict) -> dict:
    from ..api.routes_tasks import update_task
    from ..schemas import TaskIn

    task_id = int(args.get("task_id") or 0)
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return {"ok": False, "summary": f"任务 {task_id} 不存在", "data": {}, "sources": []}
        base = task.model_dump()

    allowed = ("taskname", "savepath", "quality", "auto_download", "disabled", "run_mode")
    changed = [k for k in allowed if k in args and args[k] is not None]
    for k in changed:
        base[k] = args[k]
    base.pop("runweek", None)
    body = TaskIn(**{k: v for k, v in base.items() if k in TaskIn.model_fields})
    out = await update_task(task_id, body)
    return {
        "ok": True,
        "summary": f"已更新任务「{out.taskname}」：{'、'.join(changed) or '无字段变更'}",
        "data": {"id": out.id, "changed": changed},
        "sources": [{"type": "task", "ref": f"/tasks?task_id={task_id}", "as_of": _dt(datetime.now())}],
    }


async def _retry_download(args: dict) -> dict:
    from fastapi import HTTPException

    from ..api.routes_downloads import history_retry

    record_id = int(args.get("record_id") or 0)
    try:
        res = await history_retry(record_id)
    except HTTPException as exc:
        return {"ok": False, "summary": f"重下未开始：{exc.detail}", "data": {}, "sources": []}
    return {
        "ok": True,
        "summary": res.get("message", "重下已开始，稍后刷新查看"),
        "data": {"record_id": record_id},
        "sources": [{"type": "downloads", "ref": "/downloads", "as_of": _dt(datetime.now())}],
    }


async def _run_task(args: dict) -> dict:
    from ..services.task_service import run_tasks

    task_id = int(args.get("task_id") or 0)
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return {"ok": False, "summary": f"任务 {task_id} 不存在", "data": {}, "sources": []}
        name = task.taskname
    summary = await run_tasks(task_ids=[task_id], trigger="agent")
    total = summary.get("total") or 0
    return {
        "ok": True,
        "summary": f"任务「{name}」运行结束：{json.dumps(summary, ensure_ascii=False)[:200]}",
        "data": {"task_id": task_id, "taskname": name, "total": total, "raw": summary},
        "sources": [{"type": "logs", "ref": "/logs", "as_of": _dt(datetime.now())}],
    }


_IMPL = {
    "list_tasks": _list_tasks,
    "get_task": _get_task,
    "list_downloads": _list_downloads,
    "list_accounts": _list_accounts,
    "search_resources": _search_resources,
    "check_health": _check_health,
    "browse_share": _browse_share,
    "browse_dir": _browse_dir,
    "get_settings_summary": _settings_summary,
    "create_task": _create_task,
    "update_task": _update_task,
    "run_task": _run_task,
    "retry_download": _retry_download,
}


async def execute(name: str, raw_args: str | dict, *, confirmed: bool = False) -> dict:
    """执行一个工具调用。

    只读工具直接执行；写工具在未确认时抛 ToolNeedsConfirm（由上层转成待确认动作卡）。
    """
    if name not in TOOL_NAMES:
        raise ToolForbidden(f"不支持的操作：{name}")
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args or "{}")
        except json.JSONDecodeError:
            args = {}
    else:
        args = dict(raw_args or {})
    if name in WRITE_TOOLS and not confirmed:
        raise ToolNeedsConfirm(name, args)
    try:
        return await _IMPL[name](args)
    except ToolNeedsConfirm:
        raise
    except Exception as exc:  # noqa: BLE001 工具异常必须回传给模型，不能让整轮对话崩掉
        return {"ok": False, "summary": f"{name} 执行失败：{exc}", "data": {}, "sources": []}
