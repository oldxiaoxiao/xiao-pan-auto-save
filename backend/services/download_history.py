"""下载账本：把每个下载动作的完整生命周期落到 SQLite，供历史查询、到位校验与失败重下。

写入只有两个点：start() 落 queued，finish() 写终态。实时进度（速度/已下字节）仍只在
内存 registry，不进这张表 —— 避免每 0.25s 一次写盘。
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import select

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
