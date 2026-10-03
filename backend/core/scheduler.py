"""APScheduler 定时调度：crontab 表达式 + 任务级 runweek/enddate 过滤。"""

from __future__ import annotations

import logging
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
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

    def reschedule_task(self, task_id: int, schedule: str, func) -> str | None:
        job_id = f"xiao_pan_task_{task_id}"
        if not schedule:
            self.unschedule_task(task_id)
            return None
        trigger = self._parse_trigger(schedule)
        if trigger is None:
            return None
        self.scheduler.add_job(
            func, trigger=trigger, id=job_id, replace_existing=True, max_instances=1, coalesce=True
        )
        return str(trigger)

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


def task_due_today(task, today: date | None = None) -> bool:
    """runweek（周一=1..周日=7，空=每天）与 enddate（YYYY-MM-DD）过滤。"""
    today = today or date.today()
    if getattr(task, "disabled", False):
        return False
    runweek = task.runweek_list() if hasattr(task, "runweek_list") else []
    if runweek and today.isoweekday() not in runweek:
        return False
    enddate = getattr(task, "enddate", "") or ""
    if enddate:
        try:
            if today > datetime.strptime(enddate, "%Y-%m-%d").date():
                return False
        except ValueError:
            pass
    return True
