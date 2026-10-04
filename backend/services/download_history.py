"""下载账本：把每个下载动作的完整生命周期落到 SQLite，供历史查询、到位校验与失败重下。

写入只有两个点：start() 落 queued，finish() 写终态。实时进度（速度/已下字节）仍只在
内存 registry，不进这张表 —— 避免每 0.25s 一次写盘。
"""

from __future__ import annotations

import os
import stat
from datetime import datetime

from sqlalchemy import func, or_
from sqlmodel import col, select

from ..database import session_scope
from ..models import DownloadRecord

TERMINAL = {"done", "failed", "skipped", "stopped"}


def start(
    *,
    source: str,
    ref_id: str,
    task_id: int | None,
    taskname: str,
    filename: str,
    dest_path: str,
    size_total: int,
    fid: str,
    driver_key: str,
    account_id: int | None,
) -> int:
    row = DownloadRecord(
        source=source,
        ref_id=ref_id,
        task_id=task_id,
        taskname=taskname,
        filename=filename,
        dest_path=dest_path,
        size_total=int(size_total or 0),
        fid=fid,
        driver_key=driver_key,
        account_id=account_id,
        status="queued",
    )
    with session_scope() as session:
        session.add(row)
        session.flush()
        return int(row.id)


def finish(
    ref_id: str,
    *,
    source: str,
    status: str,
    size_done: int | None = None,
    size_total: int | None = None,
    error: str = "",
) -> None:
    """按 ref_id 定位最近一条，写终态。找不到就静默返回（记录可能已被清理）。"""
    with session_scope() as session:
        row = session.exec(
            select(DownloadRecord)
            .where(DownloadRecord.ref_id == ref_id, DownloadRecord.source == source)
            .order_by(DownloadRecord.id.desc())
        ).first()
        if row is None:
            return
        row.status = status
        if size_done is not None:
            row.size_done = int(size_done)
        if size_total is not None:
            row.size_total = int(size_total)
        row.error = error or ""
        row.finished_at = datetime.now()
        session.add(row)


def file_state(path: str) -> str:
    """文件到位校验：ok=常规文件在；missing=确实不存在；unknown=读不到（权限/挂载异常），不当成丢失。"""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unknown"
    return "ok" if stat.S_ISREG(st.st_mode) else "unknown"


def _conditions(*, status: str, task_id: int | None, keyword: str) -> list:
    conds = []
    wanted = [s for s in (status or "").split(",") if s]
    if wanted:
        conds.append(col(DownloadRecord.status).in_(wanted))
    if task_id is not None:
        conds.append(col(DownloadRecord.task_id) == int(task_id))
    if keyword:
        # 转义 LIKE 通配符（先反斜杠，再 %/_），并显式声明 escape：搜 "100%" 按字面匹配，不当通配符
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{escaped}%"
        conds.append(or_(col(DownloadRecord.filename).like(like, escape="\\"),
                         col(DownloadRecord.dest_path).like(like, escape="\\")))
    return conds


def list_records(*, page: int = 1, page_size: int = 50, status: str = "", task_id: int | None = None,
                 keyword: str = "") -> dict:
    """按创建时间倒序分页查账本。file_state 由调用方（路由层）用线程池补，避免阻塞事件循环。"""
    page = max(1, int(page))
    page_size = min(200, max(1, int(page_size)))
    conds = _conditions(status=status, task_id=task_id, keyword=keyword)
    with session_scope() as session:
        total = session.exec(select(func.count()).select_from(DownloadRecord).where(*conds)).one()
        rows = session.exec(
            select(DownloadRecord)
            .where(*conds)
            .order_by(col(DownloadRecord.created_at).desc(), col(DownloadRecord.id).desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        items = [r.model_dump() for r in rows]
    return {"items": items, "total": int(total)}


def get_record(record_id: int) -> dict | None:
    with session_scope() as session:
        row = session.get(DownloadRecord, int(record_id))
        return row.model_dump() if row else None


def delete_record(record_id: int) -> bool:
    """删账本记录，不动磁盘文件。"""
    with session_scope() as session:
        row = session.get(DownloadRecord, int(record_id))
        if row is None:
            return False
        session.delete(row)
    return True


async def reconcile(cfg) -> None:
    """Task 4 实现非终态对账；此处先保持无副作用。"""
    return None
