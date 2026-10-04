"""下载账本：把每个下载动作的完整生命周期落到 SQLite，供历史查询、到位校验与失败重下。

写入只有两个点：start() 落 queued，finish() 写终态。实时进度（速度/已下字节）仍只在
内存 registry，不进这张表 —— 避免每 0.25s 一次写盘。
"""

from __future__ import annotations

import asyncio
import os
import stat
from datetime import datetime, timedelta

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


def _retention_days(retention: str) -> int | None:
    """days_90 → 90；forever / 非法值 → None（不清）。"""
    if not str(retention).startswith("days_"):
        return None
    try:
        return max(1, int(str(retention).split("_", 1)[1]))
    except ValueError:
        return None


def prune(mode: str, retention: str = "days_90") -> int:
    """清理账本。auto=按保留策略删过期终态记录；failed=只删失败；all=清空。返回删除条数。

    三种模式都只删 DownloadRecord 行，绝不碰磁盘上已下载的文件。
    auto 只收 TERMINAL：非终态记录归 reconcile 管，清理不得抢它的收口权。
    未识别的 mode 落到 auto 分支（保守：宁可少删不可删错）。
    """
    with session_scope() as session:
        if mode == "all":
            stmt = select(DownloadRecord)
        elif mode == "failed":
            stmt = select(DownloadRecord).where(col(DownloadRecord.status) == "failed")
        else:
            days = _retention_days(retention)
            if days is None:
                return 0
            cutoff = datetime.now() - timedelta(days=days)
            stmt = select(DownloadRecord).where(
                col(DownloadRecord.status).in_(TERMINAL), col(DownloadRecord.finished_at) < cutoff
            )
        rows = session.exec(stmt).all()
        for row in rows:
            session.delete(row)
        return len(rows)


STALE_HOURS = 24
ACTIVE_STATES = ("queued", "downloading")


def open_records() -> list[dict]:
    with session_scope() as session:
        rows = session.exec(select(DownloadRecord).where(col(DownloadRecord.status).in_(ACTIVE_STATES))).all()
        return [r.model_dump() for r in rows]


def _file_check(path: str, size_total: int) -> tuple[str, bool, int]:
    """兜底校验：一次 os.stat 同时给出「到位状态」与「大小是否匹配」，避免每行 stat 两次。

    返回 (state, matches, size)：
    - state 与 file_state 同语义（ok/missing/unknown；unknown 不当成丢失）；
    - matches 仅在 state == "ok" 时有意义：size_total 为 0 时非空即到位，否则须精确相符；
    - size 为实际字节数（stat 失败时 0），供 size_total 缺失时回填真实大小。
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "missing", False, 0
    except OSError:
        return "unknown", False, 0
    if not stat.S_ISREG(st.st_mode):
        return "unknown", False, 0
    matches = st.st_size == size_total if size_total else st.st_size > 0
    return "ok", matches, st.st_size


async def reconcile(cfg) -> None:
    """收口非终态记录：aria2 问 RPC → 文件 stat 兜底 → 超 24h 判失败。

    cfg 为 DownloadSettings；调用方（路由层）负责读配置，本函数不碰 setting。
    延迟导入 download_service 是为了避开与写入点的循环导入。
    """
    from ..core.download_registry import registry
    from .download_service import aria2_rpc, aria2_status

    rows = open_records()
    if not rows:
        return

    running: set[str] = set()
    results: dict[str, dict] = {}
    if cfg.mode == "aria2" and cfg.aria2_host_port:
        try:
            running = {j["id"] for j in await aria2_status(cfg)}
        except Exception:  # noqa: BLE001 aria2 不可达时静默降级到文件兜底
            running = set()
        asked = [r["ref_id"] for r in rows if r["source"] == "aria2" and r["ref_id"] not in running]
        if asked:
            try:
                resp = await aria2_rpc(
                    cfg, "system.multicall",
                    [{"methodName": "aria2.tellDownloadResult", "params": [g]} for g in asked],
                )
                # system.multicall 的返回按 asked gid 顺序一一对应
                for gid, entry in zip(asked, resp.get("result") or [], strict=False):
                    if "errorMessage" in entry:
                        continue  # gid 结果已被 aria2 丢弃，走文件兜底
                    struct = (entry.get("result") or [None])[0]
                    if struct:
                        results[gid] = struct
            except Exception:  # noqa: BLE001
                results = {}

    now = datetime.now()
    for r in rows:
        ref, source = r["ref_id"], r["source"]
        skip = False
        if source == "builtin":
            job = registry.get(ref)
            if job is None:
                pass  # 进程重启后内存清空，落到文件兜底
            elif job.status in TERMINAL:
                finish(ref, source=source, status=job.status, size_done=job.done, size_total=job.total,
                       error=job.error)
                continue
            else:
                skip = True  # 本进程还在下，进行中 tab 负责展示
        elif cfg.mode == "aria2" and cfg.aria2_host_port:
            if ref in running:
                skip = True  # aria2 仍在跑/排队，不动
            else:
                struct = results.get(ref)
                if struct is not None:
                    state = str(struct.get("status") or "")
                    if state == "complete":
                        finish(ref, source=source, status="done", size_done=int(struct.get("completedLength") or 0),
                               size_total=int(struct.get("totalLength") or r["size_total"] or 0))
                        continue
                    if state in ("error", "removed"):
                        # 结构体里错误字段两种拼写都读：snake（aria2 惯例）优先，camel 兜底
                        finish(ref, source=source, status="failed",
                               size_done=int(struct.get("completedLength") or 0),
                               error=str(struct.get("error_message") or struct.get("errorMessage") or "aria2 未成功"))
                        continue

        if skip:
            continue
        # 兜底：文件到位 = 完成（同步 IO 走线程池，一次 stat 出齐状态与大小，不卡事件循环）
        state, matches, size = await asyncio.to_thread(_file_check, r["dest_path"], int(r["size_total"] or 0))
        if state == "ok" and matches:
            # size_total 为 0 时"非空即到位"，done 用 stat 到的真实字节数，不写 0
            finish(ref, source=source, status="done", size_done=size, size_total=size)
            continue
        if now - r["created_at"] >= timedelta(hours=STALE_HOURS):
            finish(ref, source=source, status="failed",
                   error=f"对账超时：下载器无响应或结果已丢弃（超过 {STALE_HOURS} 小时未确认）")
