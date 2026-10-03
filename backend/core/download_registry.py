"""下载进度内存注册表：内置下载器写盘时更新，前端轮询快照。进程重启即清空。"""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field

ACTIVE = {"queued", "downloading"}


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
    status: str = "queued"  # queued|downloading|done|failed|skipped
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
        return job.id

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

    def snapshot(self) -> list[dict]:
        active = sorted(self._active.values(), key=lambda j: j.started_at)
        return [asdict(j) for j in active + list(self._done)]


registry = DownloadRegistry()
