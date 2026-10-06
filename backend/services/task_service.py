"""任务编排：账号选取、引擎调用、结果聚合通知。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import Session, func, select

from ..config import PROXY
from ..core.engine import TaskSpec, run_update_task
from ..core.logstream import hub
from ..core.router import route_driver
from ..core.scheduler import enddate_passed, has_valid_schedule, task_due_today
from ..database import session_scope
from ..models import Account, Task, run_mode_of

STATUS_ICONS = {
    "updated": "✅",
    "no_changes": None,
    "banned": "❌",
    "network": "⚠️",
    "failed": "❌",
}

# 全局运行锁：Phase 2 后多个 per-task 定时作业可并发调用 run_tasks；SQLite 无 WAL/busy_timeout
# 且同一夸克账号不宜被并发打。转存段（engine save + DB 写入）必须串行，下载段（本地/aria2）可并行，
# 故仅把转存段包进此锁。见 _run_tasks_inner。
_run_lock = asyncio.Lock()

ONCE_RETRY_LIMIT = 3  # 真失败最多重试三次，用尽后停摆等手动「▶ 运行」
ONCE_RETRY_DELAY_MINUTES = 5  # 三档都是 5 分钟：1 分钟低于表单自标的「建议 ≥5 分钟」风控线


def once_next_driver(task) -> str:
    """一次性任务下一步由谁驱动：retry / daily / halted / none。

    调度注册、全局扫周期、结局写库三处都只问这个函数 —— 判定散在两处是上个特性踩过的漂移源。
    判序是刻意的：预算检查必须排在 next_retry_at 之前，否则手工改库留下的矛盾态
    （用尽 + 还挂着到点时间）会被判成 retry，等于给本该停摆的行复活一条命。
    """
    if run_mode_of(task) != "once":
        return "none"
    if task.disabled:
        return "halted"  # 已完成或用户暂停，都不再自动驱动
    if enddate_passed(task):
        return "halted"
    if int(getattr(task, "retry_attempts", 0) or 0) >= ONCE_RETRY_LIMIT:
        return "halted"  # 预算用尽：停摆，等手动点运行重新给预算
    if getattr(task, "next_retry_at", None) is not None:
        return "retry"
    return "daily"


def scheduled_should_run(task) -> tuple[bool, str]:
    """定时触发（全局 crontab 或任务级作业）该不该驱动这一行，以及不驱动的原因。"""
    mode = run_mode_of(task)
    if mode == "follow":
        return True, ""
    if mode == "manual":
        return False, "仅手动"
    driver = once_next_driver(task)
    if driver == "daily":
        return True, ""
    if driver == "retry":
        return False, "等到点重试作业驱动，每日扫不让位就会双驱动"
    # halted 的三个原因按 once_next_driver 的同源判序各配一个单谓词，别再整链复制一遍：
    # disabled 最先（完成后过期=完成，不是到期），过期其次，最后才是预算用尽。
    if not task.disabled and enddate_passed(task):
        return False, "已过截止日期"
    if int(getattr(task, "retry_attempts", 0) or 0) >= ONCE_RETRY_LIMIT and not task.disabled:
        return False, "重试已用尽"
    return False, "已完成或已停用"


def load_tasks(task_ids: list[int] | None = None) -> list[Task]:
    with session_scope() as session:
        stmt = select(Task).order_by(Task.sort_order, Task.id)
        if task_ids:
            stmt = stmt.where(Task.id.in_(task_ids))
        return list(session.exec(stmt).all())


# —— 列表排序：sort_order 的取值规则集中在这儿，Web 与对外接口共用同一份 ——


def above_sort_order(session: Session) -> int:
    """排到最前：当前最小 sort_order 再前一格；空表为 0（不与任何行同值）。

    两条建任务路径都要走它：POST /api/tasks（网页表单）和 /api/add_task（油猴脚本）。
    后者漏掉时行会落回模型默认 0，被 (sort_order, id) 的 id 兜底排到列表中间，「最新在前」就破了。
    """
    current = session.exec(select(func.min(Task.sort_order))).one()
    return 0 if current is None else int(current) - 1


def below_sort_order(session: Session) -> int:
    """排到最后：当前最大 sort_order 再后一格。"""
    current = session.exec(select(func.max(Task.sort_order))).one()
    return 0 if current is None else int(current) + 1


def _task_spec(task: Task) -> TaskSpec:
    return TaskSpec(
        taskname=task.taskname,
        shareurl=task.shareurl,
        savepath=task.savepath,
        pattern=task.pattern,
        replace=task.replace,
        ignore_extension=task.ignore_extension,
        startfid=task.startfid,
        update_subdir=task.update_subdir,
        update_subdir_resave=task.update_subdir_resave,
        episode_start=task.episode_start,
        episode_end=task.episode_end,
        quality=task.quality,
    )


def _pick_account(tasks_account_id: int | None, driver_key: str) -> Account | None:
    """任务指定账号优先，否则取第一个启用且匹配驱动、有转存权限的账号。"""
    with session_scope() as session:
        if tasks_account_id:
            acc = session.get(Account, tasks_account_id)
            return acc if acc and acc.enabled else None
        accounts = session.exec(
            select(Account)
            .where(Account.enabled, Account.driver_key == driver_key)
            .order_by(Account.sort_order)
        ).all()
        return next((a for a in accounts if a.can_save), accounts[0] if accounts else None)


async def run_tasks(task_ids: list[int] | None = None, trigger: str = "manual") -> dict:
    """运行任务（全部或指定），聚合结果推送通知。返回摘要供 SSE/API 消费。

    摘要里的 driven 读作「本次处理」= 载入行数 − skipped − disabled_skipped，
    包含「没有支持的驱动」「没有可用账号」这两行根本没进 run_update_task 的失败行，别当「实际驱动」用。
    """
    run_id = uuid.uuid4().hex[:8]
    log = hub.make_logger(run_id)
    notify_lines: list[str] = []
    summary: dict = {
        "run_id": run_id,
        "trigger": trigger,
        "total": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "disabled_skipped": 0,
    }

    drivers: dict[int, object] = {}
    try:
        try:
            await _run_tasks_inner(run_id, log, drivers, notify_lines, summary, task_ids, trigger)
        finally:
            for drv in drivers.values():
                await drv.close()  # type: ignore[attr-defined]
            hub.publish("done", f"运行结束 run_id={run_id}", run_id=run_id)
    except Exception as exc:
        log("error", f"运行中断：{exc!r}")
    summary["notify_lines"] = len(notify_lines)
    # 汇总算术契约（前端 frontend/src/api/types.ts 的 RunSummary 与此互指，测试钉死）：
    #   total = driven + skipped + disabled_skipped
    # total 来自 load_tasks 的全量行数，**刻意包含停用行**；disabled_skipped 也刻意不并入 skipped，
    # 因为通知要单独向用户交代这一类跳过。driven 不自己计数，只由三者恒等推出，避免第四个键漂移。
    # 它的口径是「本次处理」，不是「实际驱动」（前端标签与此处注释同名）：
    # 「没有支持的驱动」「没有可用账号」两行只累加 failed 就 continue，既没进 skipped 也没进 disabled_skipped，
    # 于是被算进 driven —— 它们确实被这一批处理过，但一行都没进 run_update_task。
    # 与其再加第四个键，不如把名字说准；真要「进引擎的行数」看 updated/failed 或直接看日志。
    summary["driven"] = summary["total"] - summary["skipped"] - summary["disabled_skipped"]
    return summary


async def _run_tasks_inner(
    run_id: str,
    log,
    drivers: dict[int, object],
    notify_lines: list[str],
    summary: dict,
    task_ids: list[int] | None,
    trigger: str,
) -> None:
    from ..api.deps import all_settings  # 局部导入避免环依赖

    settings = all_settings()
    magic_regex = settings.get("magic_regex") or {}
    push_config = settings.get("push_config") or {}
    tasks = load_tasks(task_ids)
    summary["total"] = len(tasks)
    for task in tasks:
        tlog = hub.make_logger(run_id, task.id)
        if task.shareurl_ban:
            summary["skipped"] += 1
            tlog("warn", f"《{task.taskname}》已标记失效（{task.shareurl_ban}），跳过")
            continue
        # 全局/批量「立即运行」跳过停用任务：停用=暂停一切。行内单个「运行」按钮例外
        # （task_ids 非空即用户明确指定了这一行），便于手工重试已完成的一次性任务。
        # 必须排在 has_valid_schedule 之前：停用行即便带着有效 schedule，原因也该是「停用」而不是「独立调度」。
        # 判空用 `not task_ids` 而不是 `task_ids is None`：load_tasks 也是按真值取行的，
        # 传 [] 在它眼里就是「全部任务」，这里若按 is None 就会让 [] 变成「连停用行一起跑」，两处语义要一致。
        if task.disabled and not task_ids:
            summary["disabled_skipped"] += 1
            tlog("info", f"《{task.taskname}》已停用，本次不驱动")
            continue
        # 仅手动 / 一次性：自动触发能不能驱动这一行，只问 scheduled_should_run（判定的唯一出处）。
        if trigger == "scheduled":
            should, why = scheduled_should_run(task)
            if not should:
                summary["skipped"] += 1
                tlog("info", f"《{task.taskname}》本次不由定时器驱动：{why}")
                continue
        # 全局 sweep（task_ids 为空：None 或 []，与 load_tasks 的真值判断同口径）只驱动「无有效独立调度」的 **follow** 行：
        # follow 行自带有效 schedule 时由其专属 job 触发，避免同时被主 crontab 双驱动（如"仅周日"cron 却在每日全局点被执行）。
        # 本分支必须用 run_mode_of 限定 follow：apply_task_schedule 从不给 once / manual 行注册任务级周期作业，
        # 若也拦它们，"once + 有效 schedule + 无到点时间"这类残留调度字符串的行会被判成"daily 该扫"却又被此处跳过，
        # 落得没有任何驱动方（spec 4.3：once 无到点时间且预算未用尽 → 参与每日扫）。
        # schedule 为空或非法的 follow 行仍留在 sweep 中，继承全局回退。
        # 判空必须和上面停用那处一致用 `not task_ids`：[] 在 load_tasks 眼里就是「全部任务」，
        # 这里若按 `is None` 便会全量载入 + 不跳停用之外的一项 + 把自带调度的行双驱动，正是本分支要防的组合。
        if (
            trigger == "scheduled"
            and not task_ids
            and run_mode_of(task) == "follow"
            and has_valid_schedule(getattr(task, "schedule", "") or "")
        ):
            summary["skipped"] += 1
            tlog("info", f"《{task.taskname}》已配置独立调度（schedule={task.schedule}），全局 sweep 不再驱动")
            continue
        if trigger == "scheduled" and not task_due_today(task):
            summary["skipped"] += 1
            tlog("info", f"《{task.taskname}》按 runweek/enddate 今日不运行")
            continue

        cls = route_driver(task.shareurl)
        if cls is None:
            summary["failed"] += 1
            notify_lines.append(f"❌《{task.taskname}》：没有支持该链接的网盘驱动")
            continue
        if not cls.supported:
            summary["skipped"] += 1
            tlog("info", f"《{task.taskname}》{cls.name} 驱动即将支持，跳过")
            continue

        account = _pick_account(task.account_id, cls.key)
        if account is None:
            summary["failed"] += 1
            notify_lines.append(f"❌《{task.taskname}》：未配置可用的{cls.name}账号")
            tlog("error", f"《{task.taskname}》缺少账号")
            continue

        if account.id not in drivers:
            drivers[account.id] = cls(cookie=account.cookie, proxy=PROXY, index=account.sort_order)
        driver = drivers[account.id]  # type: ignore[assignment]

        tlog("info", f"《{task.taskname}》开始运行")
        # 转存段（engine save + DB 落库）加全局锁串行化：跨并发 run_tasks 不重叠，避免同账号并发转存与 SQLite 写冲突
        async with _run_lock:
            result = await run_update_task(driver, _task_spec(task), magic_regex=magic_regex, log=tlog)
            # 运行时间落库是转存的附带记账：SQLite 无 WAL/busy_timeout（database.py），并发写会抛锁冲突。
            # 口径对齐 _settle_once（0ed6400）：失败只记 warn，绝不抛回循环拖垮整批、丢掉 notify_lines；
            # 但转存结果 result 已在上面拿到，本段失败不掩盖已成功的转存。锁的串行段不能动（项目硬约束）。
            try:
                with session_scope() as session:
                    row = session.get(Task, task.id)
                    if row:
                        row.last_run_at = datetime.now()
                        if result.status == "banned":
                            row.shareurl_ban = result.message
                        session.add(row)
            except Exception as exc:  # noqa: BLE001 落库失败不影响本次转存结果与本批剩余任务
                tlog("warn", f"《{task.taskname}》运行时间落库失败（不影响本次结果）：{exc}")

        icon = STATUS_ICONS.get(result.status)
        # 一次性判定要覆盖所有状态（含 no_changes / 转存失败），故计数先给默认值，
        # 只有真正走了下载才在下面覆盖；收口统一在分支之后调一次。
        counts = DownloadCounts()
        if result.status == "updated":
            summary["updated"] += 1
            notify_lines.append(f"✅《{task.taskname}》添加追更：\n{result.render()}")
            tlog("info", f"《{task.taskname}》新增 {len(result.files)} 项")
            if getattr(task, "auto_download", False):
                # 下载在锁外：本地/aria2 可与其它任务的转存并行，不占用转存串行段
                counts = await _download_for_task(
                    driver, task, result, settings, notify_lines, tlog, account_id=account.id
                )
        elif result.status == "no_changes":
            tlog("info", f"《{task.taskname}》没有新的转存")
        else:
            summary["failed"] += 1
            notify_lines.append(f"{icon}《{task.taskname}》：{result.message}")
            tlog("error", f"《{task.taskname}》{result.status}：{result.message}")
        # 收口只此一处：非 once / 已停用的行由 _once_verdict 与 _settle_once 的守卫拦掉，不会多打日志；
        # 它自己吞掉写库异常（见 _settle_once 文档），所以这里裸调用也不会中断本批剩余任务与通知。
        _settle_once(task, result, counts, tlog)

    # spec 4.4：停用行是「按用户意愿没跑」，必须在通知里显式交代数量，否则用户会以为漏跑。
    # 只在非零时出现，避免「跳过 0 个已停用任务」这种噪音。
    if summary["disabled_skipped"]:
        notify_lines.append(f"⏸️ 本次跳过 {summary['disabled_skipped']} 个已停用任务")

    if notify_lines:
        await _push("小盘自动转存运行结果", "\n".join(notify_lines), push_config, settings, log)


@dataclass
class DownloadCounts:
    """一次任务运行的下载结果计数：executed 表示是否真的走到了下载器。"""

    attempted: int = 0
    ok: int = 0
    failed: int = 0
    executed: bool = False


async def _download_for_task(driver, task, result, settings, notify_lines, tlog, *, account_id=None) -> DownloadCounts:
    from .download_service import DownloadSettings, download_task_files

    counts = DownloadCounts()
    if not driver.has("download"):
        tlog("warn", f"《{task.taskname}》{driver.name} 驱动不支持下载，跳过")
        return counts
    cfg = DownloadSettings.from_dict(settings.get("download"))
    try:
        lines = await download_task_files(
            driver,
            result.files,
            cfg,
            download_subdir=bool(getattr(task, "download_subdir", False)),
            savepath_override=getattr(task, "download_savepath", "") or "",
            log=tlog,
            task_id=task.id,
            taskname=task.taskname,
            account_id=account_id,
        )
    except Exception as exc:  # noqa: BLE001 下载失败不影响转存结果
        tlog("error", f"《{task.taskname}》下载异常：{exc}")
        lines = [f"❌ 下载异常: {exc}"]
    counts.executed = True
    counts.attempted = len(lines)
    # 前缀约定沿用通知聚合的口径：✅ 开头即成功（含「✅ 跳过（已存在）」，文件本就在盘上）
    counts.ok = sum(1 for line in lines if line.startswith("✅"))
    counts.failed = counts.attempted - counts.ok
    if lines:
        notify_lines.append(
            f"📥《{task.taskname}》本地下载 {counts.ok}/{counts.attempted}：\n" + "\n".join(lines)
        )
    return counts


def _once_verdict(task, result, counts: DownloadCounts) -> tuple[bool, str]:
    """一次性任务是否算"跑完"：拿到新增资源，且（没开下载 或 下载实际执行且零失败）。"""
    if run_mode_of(task) != "once":
        return False, "非一次性任务"
    if task.disabled:
        return False, "已停用（含此前自动完成），不重复收口"
    if result.status != "updated":
        if result.status == "no_changes":
            return False, "本次没有新增资源"
        return False, f"转存未成功（{result.status}）"
    if getattr(task, "auto_download", False):
        if not counts.executed or counts.attempted == 0:
            return False, "下载未实际执行（驱动不支持或没有待下载文件）"
        if counts.failed:
            # spec 4.3：失败提示要指向下载页，用户在那儿能逐条重下，而不是只会重跑整个任务
            return False, f"{counts.failed} 项下载失败（可到下载页逐条重下）"
    return True, "已获取新增资源"


def _settle_once(task, result, counts: DownloadCounts, tlog) -> None:
    """一次性任务的三种结局：拿到手 → 停用；还没放出 → 不占预算；真失败 → 吃一次预算。

    这是旁路记账：落库失败只记一条 warn，绝不抛回运行循环——否则本批余下任务不跑，
    已生成的 notify_lines 也一起丢掉，而前端只看到干净的 done。口径同下载账本。
    """
    if run_mode_of(task) != "once":
        return
    done, reason = _once_verdict(task, result, counts)
    now = datetime.now()
    try:
        with session_scope() as session:
            row = session.get(Task, task.id)
            if row is None or row.disabled:
                return  # 运行中被删/已被别处停用：不动作
            mode_now = run_mode_of(row)
            if mode_now != "once":
                return  # 期间用户改回「定时追更」：不再由这里停用或计数
            if done:
                row.disabled = True
                row.retry_attempts = 0
                row.next_retry_at = None
                action = "info"
                msg = f"《{row.taskname}》一次性任务已完成并自动停用（{reason}）"
            elif result.status == "no_changes":
                row.next_retry_at = None  # 还没放出：不占预算，等下一次每日扫再来看
                action = "info"
                msg = f"《{row.taskname}》本次没有新增资源（还没放出或早已转存过），不占重试预算，等下次定时检查"
            else:
                row.retry_attempts = int(row.retry_attempts or 0) + 1
                if row.retry_attempts >= ONCE_RETRY_LIMIT:
                    row.next_retry_at = None
                    action = "warn"
                    msg = (
                        f"《{row.taskname}》三次重试仍未成功（{reason}），已停止自动重试；"
                        "点该行「▶ 运行」可重新开始"
                    )
                elif enddate_passed(row):
                    # 过期行只记账不排格：once_next_driver 见过期就判 halted，排了也没人跑，
                    # 留下一格 `next_retry_at` 就是本特性刻意堵死的那种矛盾态。判定出处仍是这
                    # 一个单谓词，不复制 once_next_driver 的整条链。
                    row.next_retry_at = None
                    action = "warn"
                    msg = (
                        f"《{row.taskname}》本次未成功（{reason}），已过截止日期，不再排重试"
                        f"（{row.retry_attempts}/{ONCE_RETRY_LIMIT}）；点该行「▶ 运行」仍可手动跑一次"
                    )
                else:
                    row.next_retry_at = now + timedelta(minutes=ONCE_RETRY_DELAY_MINUTES)
                    action = "warn"
                    msg = (
                        f"《{row.taskname}》本次未成功（{reason}），"
                        f"{ONCE_RETRY_DELAY_MINUTES} 分钟后重试（{row.retry_attempts}/{ONCE_RETRY_LIMIT}）"
                    )
            session.add(row)
    except Exception as exc:  # noqa: BLE001 旁路记账
        tlog("warn", f"《{task.taskname}》一次性收口失败（不影响运行）：{exc}")
        return
    tlog(action, msg)
    _resync_schedule(task.id)  # 写完到点时间/停用，必须让作业与数据对齐，否则重启前这一格没人跑


def _resync_schedule(task_id: int) -> None:
    """写完 next_retry_at / disabled 后让调度器与数据对齐；导不到就只记日志，不影响主流程。"""
    try:
        from ..main import apply_task_schedule

        with session_scope() as session:
            row = session.get(Task, task_id)
        if row is not None:
            apply_task_schedule(row)
    except Exception as exc:  # noqa: BLE001
        hub.publish("warn", f"任务 {task_id} 调度同步失败：{exc}")


def reset_once_budget(task_id: int) -> None:
    """行内「▶ 运行」= 重新给三次预算；只对 once 行生效，不偷偷取消停用。

    清 next_retry_at 之后必须把已排上的到点作业也撤掉：否则这一格稍后还会自己跑一次，
    用户看到的就成了"点一下运行，五分钟后又莫名跑了一次"。
    """
    try:
        with session_scope() as session:
            row = session.get(Task, task_id)
            if row is None or run_mode_of(row) != "once":
                return
            had_slot = row.next_retry_at is not None
            row.retry_attempts = 0
            row.next_retry_at = None
            session.add(row)
        if had_slot:
            from ..main import scheduler

            scheduler.unschedule_retry(task_id)
    except Exception as exc:  # noqa: BLE001 旁路写库
        hub.publish("warn", f"任务 {task_id} 重置重试预算失败：{exc}")


async def _push(title: str, content: str, push_config: dict, settings: dict, log) -> None:
    if not settings.get("notify_enabled", True):
        return
    from .notify_service import push_all  # 延迟导入，模块由并行任务交付

    try:
        for channel, ok, message in await push_all(title, content, push_config, log=log):
            if not ok:
                log("warn", f"通知渠道 {channel} 失败：{message}")
    except Exception as exc:  # noqa: BLE001
        log("error", f"通知发送异常：{exc}")
