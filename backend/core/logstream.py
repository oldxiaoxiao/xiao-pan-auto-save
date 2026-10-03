"""结构化运行日志：同时写文件与 SSE 订阅队列。"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..config import LOG_DIR

LogFn = Callable[..., None]


class LogHub:
    """运行日志中心：publish 双写（文件 + 各订阅队列），history 保留最近 N 条。"""

    def __init__(self, log_dir: Path = LOG_DIR, keep: int = 1000):
        self.log_dir = log_dir
        self.history: deque[dict] = deque(maxlen=keep)
        self.subscribers: set[asyncio.Queue] = set()

    def publish(self, level: str, message: str, run_id: str = "", task_id: int | None = None) -> None:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "level": level.lower().replace("warning", "warn"),  # 归一：info/warn/error/done/summary
            "message": re.sub(r"\s+", " ", message)[:4000],
            "run_id": run_id,
            "task_id": task_id,
        }
        self.history.append(entry)
        self._write_file(entry)
        for q in list(self.subscribers):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass  # 慢消费者丢弃，不阻塞运行

    def _write_file(self, entry: dict) -> None:
        path = self.log_dir / f"runtime-{datetime.now():%Y-%m-%d}.log"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def make_logger(self, run_id: str = "", task_id: int | None = None) -> LogFn:
        def log(level: str, message: str, **kw) -> None:
            self.publish(
                level, str(message), run_id=kw.get("run_id", run_id), task_id=kw.get("task_id", task_id)
            )

        return log

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)


hub = LogHub()
