"""APScheduler 定时调度：crontab 表达式 + 任务级 runweek/enddate 过滤。"""

from __future__ import annotations

import logging
from datetime import date, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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
