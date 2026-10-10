"""下载账本：把每个下载动作的完整生命周期落到 SQLite，供历史查询、到位校验与失败重下。

写入只有两个点：start() 落 queued，finish() 写终态。实时进度（速度/已下字节）仍只在
内存 registry，不进这张表 —— 避免每 0.25s 一次写盘。
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from datetime import datetime, timedelta

from sqlalchemy import func, or_
from sqlmodel import col, select

from ..database import session_scope
from ..models import DownloadRecord

logger = logging.getLogger(__name__)

# interrupted 也属终态：它是"这次尝试已经结束（被中断）"，只是还能从断点再开一次。
# 不放进 TERMINAL 的话，这些行会一直挂在「进行中」，既不能被重下（has_open_for_path 拦），
# 也要等到 24h 的 STALE 规则才被判失败——用户两头都看不到"被中断"这件事。
TERMINAL = {"done", "failed", "skipped", "stopped", "interrupted"}
INTERRUPTED = "interrupted"


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


def file_state(path: str, expected_size: int = 0, terminal: bool = True) -> str:
    """文件到位校验（历史查询用，语义与 _file_check 完全同源，只有一处 stat）。

    - 非终态行（terminal=False，即 queued/downloading）：一律 unknown（UI「未校验」）。
      在途文件可能是 aria2 的预分配占位，"在不在"根本不说明问题——活体发现过 1097 字节
      占位配 3.5GB 预期被报成 ok（2026-10-05）。非终态不报 ok 之外还省掉一次 stat。
    - 终态行：常规文件、大小与账本相符、既没有 <name>.aria2 控制文件也不是稀疏预分配 → ok；
      常规文件在但大小不符、或带着 aria2 的控制文件、或只有预分配的大小没有实际字节 → partial
      （UI「不完整」）；不存在 → missing；权限/挂载异常或非常规文件 → unknown；
      账本没记大小（expected_size=0）时状态照旧报 ok（文件确实在），但不算「验证过的完整」
      （matches=False）：UI 之外没有任何调用方可以据此跳过或收口。
    """
    return _file_check(path, expected_size, terminal)[0]


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


def _part_bytes(dest_path: str) -> int:
    """磁盘上 `.part` 已下到的字节数——进程重启后唯一可信的进度来源。"""
    try:
        return os.stat(str(dest_path) + ".part").st_size
    except OSError:
        return 0


def mark_interrupted() -> int:
    """把内置下载的在途账本行收口为 `interrupted`（FR-07 队列持久化）。

    进度原本只活在两个地方：内存 registry（重启即清空）与磁盘上的 `.part`。
    重启后前者没了，这些行如果不收口，就会一直挂在"进行中"——界面显示永远转圈，
    重下入口还被 `has_open_for_path` 堵着，直到 24h 后被 STALE 规则判成失败。

    aria2 的行不动：它自己有持久化与断点续传，且 reconcile 会逐个 gid 问 tellStatus，
    在这里抢先判"中断"反而会盖掉它真实的在途状态。
    """
    moved = 0
    now = datetime.now()
    with session_scope() as session:
        rows = session.exec(
            select(DownloadRecord).where(col(DownloadRecord.status).in_(ACTIVE_STATES))
        ).all()
        for row in rows:
            if row.source != "builtin":
                continue
            got = _part_bytes(row.dest_path)
            total = int(row.size_total or 0)
            if got and total:
                row.error = f"程序中断，已下 {got / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f}MB（{got / total * 100:.0f}%），可从断点继续"
            elif got:
                row.error = f"程序中断，已下 {got / 1024 / 1024:.1f}MB（总大小未知，无法续传）"
            else:
                row.error = "程序中断，尚未写入数据"
            row.size_done = got
            row.status = INTERRUPTED
            row.finished_at = now
            session.add(row)
            moved += 1
    return moved


def open_records() -> list[dict]:
    with session_scope() as session:
        rows = session.exec(select(DownloadRecord).where(col(DownloadRecord.status).in_(ACTIVE_STATES))).all()
        return [r.model_dump() for r in rows]


def has_open_for_path(dest_path: str) -> bool:
    """同 dest_path 是否存在 STALE_HOURS 内创建的非终态账本行（含被查记录自身）。

    重下入口用它把关：两个内置写者并发写同一个 <name>.part 会交错字节、双重改名，把文件写坏。
    超过 STALE_HOURS 的悬挂行不在这里拦——那是 reconcile 的收口职责，收口后自然放行。
    """
    cutoff = datetime.now() - timedelta(hours=STALE_HOURS)
    return any(r["dest_path"] == dest_path and r["created_at"] > cutoff for r in open_records())


BLOCK_BYTES = 512  # st_blocks 的单位：macOS/Linux 都按 512 字节计（POSIX 约定，与块大小无关）


def _aria2_control_present(path: str) -> bool:
    """同目录是否留着 <文件名>.aria2 控制文件——aria2 在途/半途而废的铁证。

    aria2 下载一个文件时会在目标旁建控制文件（活体实测 1097 字节），收尾才删。它还在就说明
    这次下载没完成，此时磁盘上的字节数毫无意义。读控制文件本身出错（挂载抖动）时也判 True：
    判不了就绝不轻说"完整"，宁可多下一次。
    """
    try:
        os.stat(path + ".aria2")
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _is_sparse(st: os.stat_result) -> bool:
    """声明了大小却没真占块 = 预分配占位（aria2 --file-allocation 默认 prealloc 就这样）。

    稀疏文件把整片大小「免费」摊出来，所以 st_size == 预期完全不代表下完。
    注：块数报不准的文件系统（个别 FUSE/CIFS 挂载、Windows、透明压缩的 btrfs）会把完整文件也
    读成稀疏，后果是多下一次而绝不会假跳过——判不准时一律倒向"重新下"，不把垃圾认成成品。
    """
    return st.st_blocks * BLOCK_BYTES < st.st_size


def _file_check(path: str, size_total: int, terminal: bool = True) -> tuple[str, bool, int]:
    """全项目唯一「stat 并解读一个路径」的地方：一次 os.stat 同时给出到位状态、大小相符与真实字节。

    返回 (state, matches, size)：
    - state：ok/missing/unknown/partial（partial=常规文件在但不是验证过的完整文件，仅终态行会出现）；
    - matches：**这个文件是不是"验证过的完整文件"**，唯一可以据此跳过/收口的凭据。四条全满足才 True：
      常规文件、账本记了大小且 st_size == size_total、同目录没有 <name>.aria2 控制文件、
      不是稀疏预分配（st_blocks*512 < st_size）。大小未知（0）时无从校验 → 一律 False，
      两种模式都当"没验证过"重新下（评审 Important 3：旧的"非空即到位"让 aria2 模式把残留
      半截文件永久假跳过，内置模式却重下，同一判据分叉成两套）；
    - size 为实际字节数（stat 失败或非终态时 0），供日志与 UI 展示。
    - terminal=False（queued/downloading 行）：在途文件可能是 aria2 预分配占位，任何"到位"
      结论都是撒谎，直接 unknown 并跳过 stat（活体 2026-10-05：1097 字节占位被报成 ok）。
    file_state 取 state 给 UI；aria2 投递前预检、内置 _fetch_one 的短路、reconcile 的兜底
    都用 state + matches —— 四个消费点一份判据，不再有任何变体。
    """
    if not terminal:
        return "unknown", False, 0
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "missing", False, 0
    except OSError:
        return "unknown", False, 0
    if not stat.S_ISREG(st.st_mode):
        return "unknown", False, 0
    size = st.st_size
    if size_total and size != size_total:
        return "partial", False, size
    # 大小对得上也仍是占位：控制文件在 = aria2 还/曾在这条路径上干活；稀疏 = 声明大小没有字节
    if _aria2_control_present(path) or _is_sparse(st):
        return "partial", False, size
    return "ok", bool(size_total) and size == size_total, size


async def reconcile(cfg) -> None:
    """收口非终态记录：aria2 逐 gid 问 tellStatus → 「验证过的完整文件」兜底 → 超 24h 判失败。

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
        except Exception as exc:  # noqa: BLE001 aria2 不可达时静默降级到文件兜底
            # 留痕：运维要能区分「aria2 不可达」与「gid 已被 aria2 丢弃」，静默降级两头都看不出来
            logger.debug("对账时查询 aria2 活动队列失败，降级到文件兜底：%s", exc)
            running = set()
        asked = [r["ref_id"] for r in rows if r["source"] == "aria2" and r["ref_id"] not in running]
        if asked:
            # 真机 aria2 1.36.0 实测：listMethods 里没有 aria2.tellDownloadResult，
            # system.multicall 又拒绝 aria2_rpc 前置的 token（"The parameter at 0 has wrong type"），
            # 所以终态只能逐个 gid 问 aria2.tellStatus。显式列 keys：不传时真机会把整个 files[]
            # （几百条 uri）带回来，对账一个字段也用不上。
            keys = ["gid", "status", "totalLength", "completedLength", "errorCode", "errorMessage"]
            # 并发上限：asked 正常只有几条，但后端崩溃重启后可能一次攒出成百上千行非终态记录，
            # 而 reconcile 是在历史查询的 HTTP 请求里 await 的——不加闸会瞬间开出等量 httpx 连接
            gate = asyncio.Semaphore(16)

            async def ask(gid: str) -> dict | None:
                """问单个 gid 的结果体；问不到（不可达 / JSON-RPC error）返回 None 交给文件兜底。"""
                try:
                    async with gate:
                        resp = await aria2_rpc(cfg, "aria2.tellStatus", gid, keys)
                except Exception as exc:  # noqa: BLE001 daemon 不可达时静默降级到文件兜底
                    logger.debug("对账时询问 aria2 tellStatus 异常（gid %s），降级到文件兜底：%s", gid, exc)
                    return None
                if "error" in resp:
                    # gid 结果已被 aria2 丢弃或从未存在：真机给的是 JSON-RPC error 体（实测
                    # {"error":{"code":1,"message":"GID xxx is not found"}}），不是异常
                    logger.debug("aria2 查不到 gid %s（%s），降级到文件兜底", gid, resp.get("error"))
                    return None
                return resp.get("result") or None

            # gid 集合通常很小（非终态且不在活动队列），并发问完即可；gather 保序，
            # 结果按 asked 里的 gid 归档，不存在按位置错配到别行的可能
            structs = await asyncio.gather(*(ask(g) for g in asked), return_exceptions=True)
            for gid, struct in zip(asked, structs, strict=False):
                if isinstance(struct, dict):
                    results[gid] = struct

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
                        # 失败理由：真机字段是 camelCase errorMessage，snake_case 兼容；
                        # 两者皆空但有非零 errorCode 时把码带上，至少这行可诊断
                        reason = str(struct.get("errorMessage") or struct.get("error_message") or "").strip()
                        code = str(struct.get("errorCode") or "").strip()
                        if not reason:
                            reason = f"aria2 未成功（errorCode {code}）" if code not in ("", "0") else "aria2 未成功"
                        finish(ref, source=source, status="failed",
                               size_done=int(struct.get("completedLength") or 0), error=reason)
                        continue
                    if state in ("active", "waiting", "paused"):
                        skip = True  # 仍在跑/排队（正常情况下这些 gid 来自 aria2_status），绝不据此收口

        if skip:
            continue
        # 兜底：文件是「验证过的完整文件」才算完成（同步 IO 走线程池，一次 stat 出齐状态与大小，
        # 不卡事件循环）。这里的行按定义都是非终态（open_records 只给 queued/downloading），也就是
        # "下载可能还在进行"，而 gid 这次问不到有太多种无害解释：daemon 重启丢了结果缓存、后端
        # 切到内置模式、状态串不认识。评审 Important 2（活体）：旧实现只比 st_size == size_total，
        # 于是 aria2 预分配出来的整片占位会把在途作业当场收口成 done —— 同一批提交刚在 UI 上宣布
        # 这种占位不作数。收口只认 _file_check 的完整结论（稀疏与 .aria2 控制文件都已被它排除，
        # 大小未知的行也验证不了），确认不了的留给下面 24h 规则这个唯一的终态退路。
        # 这里传 terminal=True 不是把行当终态（这批行全是非终态），只是打开"允许对文件下结论"
        # 那层开关；放行之后还得过 matches 这道闸，占位/稀疏/大小未知都判不出 done。
        state, matches, size = await asyncio.to_thread(
            _file_check, r["dest_path"], int(r["size_total"] or 0), terminal=True
        )
        if state == "ok" and matches:
            # matches 蕴含 size == size_total（大小未知时根本不会 True），done 直接用 stat 到的字节
            finish(ref, source=source, status="done", size_done=size, size_total=size)
            continue
        if now - r["created_at"] >= timedelta(hours=STALE_HOURS):
            finish(ref, source=source, status="failed",
                   error=f"对账超时：下载器无响应或结果已丢弃（超过 {STALE_HOURS} 小时未确认）")
