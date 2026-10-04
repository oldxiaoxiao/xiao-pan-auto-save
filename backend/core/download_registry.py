"""下载进度内存注册表：内置下载器写盘时更新，前端轮询快照。进程重启即清空。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field

ACTIVE = {"queued", "downloading"}

# 每个 job 的协作式取消 Event：stop() 置位，_fetch_one 的 chunk 循环检查；终态即清理。
_controls: dict[str, asyncio.Event] = {}


@dataclass
class DownloadJob:
    id: str
    task_id: int | None
    taskname: str
    filename: str
    dest_path: str
    total: int
    done: int = 0
    speed: float = 0.0
    status: str = "queued"  # queued|downloading|done|failed|skipped|stopped（paused 仅 aria2 映射）
    error: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class DownloadRegistry:
    def __init__(self, history: int = 200) -> None:
        self._active: dict[str, DownloadJob] = {}
        self._done: deque[DownloadJob] = deque(maxlen=history)

    def create(self, *, task_id: int | None, taskname: str, filename: str, dest_path: str, total: int) -> str:
        job = DownloadJob(
            id=uuid.uuid4().hex[:12],
            task_id=task_id,
            taskname=taskname,
            filename=filename,
            dest_path=dest_path,
            total=int(total or 0),
        )
        self._active[job.id] = job
        _controls[job.id] = asyncio.Event()
        return job.id

    def get(self, job_id: str) -> DownloadJob | None:
        """按 id 取 job（含已进 _done 的终态），供服务层落账本。"""
        job = self._active.get(job_id)
        if job is not None:
            return job
        return next((j for j in self._done if j.id == job_id), None)

    def cancel_requested(self, job_id: str) -> bool:
        ev = _controls.get(job_id)
        return bool(ev and ev.is_set())

    def stop(self, job_id: str) -> bool:
        ev = _controls.get(job_id)
        if ev is None:
            return False
        ev.set()
        return True

    def remove(self, job_id: str) -> bool:
        # 幂等清理：从 active + done 移除记录并删除取消 Event，恒返回 True
        _controls.pop(job_id, None)
        self._active.pop(job_id, None)
        self._done = deque((j for j in self._done if j.id != job_id), maxlen=self._done.maxlen)
        return True

    def update(
        self,
        job_id: str,
        *,
        done: int | None = None,
        total: int | None = None,
        speed: float | None = None,
        status: str | None = None,
        error: str | None = None,
    ) -> None:
        job = self._active.get(job_id)
        if job is None:
            return
        if done is not None:
            job.done = int(done)
        if total is not None:
            job.total = int(total)
        if speed is not None:
            job.speed = float(speed)
        if error is not None:
            job.error = error
        job.updated_at = time.time()
        if status is not None:
            job.status = status
            if status not in ACTIVE:
                self._active.pop(job_id, None)
                self._done.appendleft(job)
                _controls.pop(job_id, None)  # 终态清理取消 Event，避免残留

    def snapshot(self) -> list[dict]:
        active = sorted(self._active.values(), key=lambda j: j.started_at)
        return [{**asdict(j), "source": "builtin"} for j in active + list(self._done)]


registry = DownloadRegistry()
