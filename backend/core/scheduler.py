"""APScheduler 定时调度：crontab 表达式 + 任务级 runweek/enddate 过滤。"""

from __future__ import annotations

import logging
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

logging.getLogger("apscheduler").setLevel(logging.WARNING)

MAIN_JOB_ID = "xiao_pan_main_run"


class TaskScheduler:
    def __init__(self):
        self.scheduler = AsyncIOScheduler(timezone=None)

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def reschedule(self, crontab: str, func) -> str:
        """(重新)设置主运行任务；crontab 非法时回退默认每天 09:00。"""
        try:
            trigger = CronTrigger.from_crontab(crontab)
        except (ValueError, KeyError, RuntimeError):
            trigger = CronTrigger.from_crontab("0 9 * * *")
        self.scheduler.add_job(
            func, trigger=trigger, id=MAIN_JOB_ID, replace_existing=True, max_instances=1, coalesce=True
        )
        return str(trigger)

    def add_daily(self, job_id: str, func, hour: int = 4) -> None:
        """注册一个每天整点执行的维护任务（重复注册覆盖）。"""
        self.scheduler.add_job(
            func, trigger=CronTrigger(hour=hour, minute=0), id=job_id, replace_existing=True,
            max_instances=1, coalesce=True,
        )

    def reschedule_task(self, task_id: int, schedule: str, func) -> str | None:
        job_id = f"xiao_pan_task_{task_id}"
        if not schedule:
            self.unschedule_task(task_id)
            return None
        trigger = self._parse_trigger(schedule)
        if trigger is None:
            # 非法 schedule：撤销旧 job，避免编辑后仍按旧调度触发；回退由全局 sweep 接管
            self.unschedule_task(task_id)
            logging.getLogger(__name__).warning("任务 %s 的 schedule 非法，忽略注册：%s", task_id, schedule)
            return None
        self.scheduler.add_job(
            func, trigger=trigger, id=job_id, replace_existing=True, max_instances=1, coalesce=True
        )
        return str(trigger)

    def reschedule_retry_at(self, task_id: int, when: datetime, func) -> str:
        """排一次"到点就跑"的重试作业；到点执行后由结局判定决定是否再排。

        misfire_grace_time 显式传 None（不设宽限）：调度器默认宽限只有 1 秒，停机跨过到期点或
        事件循环卡顿超过 1 秒时，过期作业会被 APScheduler 打一条 "was missed" 后直接丢弃并移除
        —— 库里的格子还在 ⇒ once_next_driver 答 "retry" ⇒ 每日扫永远让位 ⇒ 这一行永久失去驱动方，
        界面还挂着「重试中」和一个已过去的 ETA。不设宽限=迟到多久都立刻补跑，正是 4.3
        「重启后按库内 next_retry_at 重建」要的补跑语义。主 crontab 与任务级周期作业不受影响。
        """
        job_id = f"xiao_pan_retry_{task_id}"
        self.scheduler.add_job(
            func, trigger=DateTrigger(run_date=when), id=job_id, replace_existing=True,
            max_instances=1, coalesce=True, misfire_grace_time=None,
        )
        return job_id

    def unschedule_retry(self, task_id: int) -> None:
        job_id = f"xiao_pan_retry_{task_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    def unschedule_task(self, task_id: int) -> None:
        job_id = f"xiao_pan_task_{task_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)

    @staticmethod
    def _parse_trigger(schedule: str):
        if schedule.startswith("interval:"):
            try:
                mins = int(schedule.split(":", 1)[1])
            except ValueError:
                return None
            return IntervalTrigger(minutes=max(1, mins))
        if schedule.startswith("cron:"):
            try:
                return CronTrigger.from_crontab(schedule.split(":", 1)[1])
            except (ValueError, KeyError, RuntimeError):
                return None
        return None


def has_valid_schedule(schedule: str) -> bool:
    """schedule 非空且能解析出有效 trigger（即任务自带独立调度）。复用 TaskScheduler._parse_trigger。"""
    return bool(schedule) and TaskScheduler._parse_trigger(schedule) is not None


def enddate_passed(task, today: date | None = None) -> bool:
    """截止日期是否已过；空或非法日期一律 False（不挡路），与 task_due_today 既有口径一致。"""
    enddate = getattr(task, "enddate", "") or ""
    if not enddate:
        return False
    try:
        return (today or date.today()) > datetime.strptime(enddate, "%Y-%m-%d").date()
    except ValueError:
        return False


def task_due_today(task, today: date | None = None) -> bool:
    """runweek（周一=1..周日=7，空=每天）与 enddate（YYYY-MM-DD）过滤。"""
    today = today or date.today()
    if getattr(task, "disabled", False):
        return False
    runweek = task.runweek_list() if hasattr(task, "runweek_list") else []
    if runweek and today.isoweekday() not in runweek:
        return False
    if enddate_passed(task, today):
        return False
    return True
