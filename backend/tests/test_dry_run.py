"""试跑（plan_only）与真实运行必须给出同一批结论；plan_only 不得产生任何写。"""

from __future__ import annotations

import asyncio

from backend.core.engine import TaskSpec, run_update_task
from backend.drivers.base import FsItem, SaveResult
from backend.tests.test_task_service import OkDriver

_ROOT = ["01.4K.SDR.mp4", "02.4K.SDR.mp4", "03.4K.SDR.mp4", "特典花絮.mp4", "预告片.mp4"]
_CHILD = ["11.4K.SDR.mp4", "12.4K.SDR.mp4", "花絮A.mp4"]


class PlanDriver(OkDriver):
    """两层假目录树：根里 5 个文件 + 一个「合集」目录；目标目录里**已经有**「合集」。

    这一步是刻意的：递归只发生在"目标已存在这个目录"的追更场景（engine.py:215 的 elif），
    目录不存在时它整目录搬走（engine.py:207-210），所以想让试跑真的进目录，必须先把目录摆成已存在。
    写操作全部计数，用来证明 plan_only 一次都不写。
    """

    capability = {"rename", "delete", "mkdir"}
    calls: dict[str, int] = {}

    def _bump(self, name: str) -> None:
        PlanDriver.calls[name] = PlanDriver.calls.get(name, 0) + 1

    async def list_share(self, ref, path=""):
        if not path:
            items = [FsItem(fid=f"r{i}", name=n, size=1000 + i, mtime=i) for i, n in enumerate(_ROOT)]
            items.append(FsItem(fid="d1", name="合集", is_dir=True, size=0, mtime=99))
            return items
        return [FsItem(fid=f"c{i}", name=n, size=2000 + i, mtime=i) for i, n in enumerate(_CHILD)]

    async def list_dir(self, path):
        if path.endswith("/剧"):
            return [FsItem(fid="x1", name="合集", is_dir=True)]  # 目标里已有这个目录
        return []

    async def ensure_dir(self, path):
        self._bump("ensure_dir")

    async def save(self, items, dest_path, ref=None):
        self._bump("save")
        return SaveResult(ok=True, saved=[FsItem(fid=f"s{i}", name=item.name, is_dir=item.is_dir, size=item.size)
                                          for i, item in enumerate(items)])

    async def rename(self, fid, name):
        self._bump("rename")

    async def delete_items(self, items, purge=True):
        self._bump("delete_items")


def _spec(**kw) -> TaskSpec:
    base = dict(taskname="剧", shareurl="https://pan.quark.cn/s/x", savepath="/剧")
    base.update(kw)
    return TaskSpec(**base)


def _plan(**kw):
    PlanDriver.calls = {}
    return asyncio.run(run_update_task(PlanDriver(), _spec(**kw), plan_only=True))


def _real(**kw):
    PlanDriver.calls = {}
    return asyncio.run(run_update_task(PlanDriver(), _spec(**kw)))


def _keyset(result):
    """比较键只用 (final_name, dest_path)：**不比整个对象**，因为 plan_only 拿不到转存回执里的 new_fid。"""
    return sorted((f.final_name, f.dest_path) for f in result.files)


def test_dry_run_and_real_run_agree_on_the_same_set():
    """承重测试：同一份 spec，试跑与真实运行必须挑出完全相同的一批。"""
    planned, real = _plan(), _real()
    assert _keyset(planned) and _keyset(planned) == _keyset(real)
    assert planned.status == "updated" == real.status
    # 根目录那 5 个文件入选；「合集」因目标已存在且不递归而被跳过（不是 bug，是既有语义）
    assert {f.share_name for f in planned.files} == set(_ROOT)


def test_plan_only_writes_nothing():
    planned = _plan()
    assert PlanDriver.calls == {}, f"plan_only 居然写了：{PlanDriver.calls}"
    assert planned.files and all(f.new_fid == "" for f in planned.files)
    assert all(f.size > 0 for f in planned.files if not f.is_dir)  # total_size 的来源，别永远是 0


def test_real_run_still_writes():
    real = _real()
    assert PlanDriver.calls.get("save", 0) == 1 and _keyset(real)
    assert all(f.new_fid for f in real.files)


def test_counts_filtered_out_and_existing_files():
    planned = _plan(episode_start=1, episode_end=3, quality="4k")
    assert {f.share_name for f in planned.files} == {"01.4K.SDR.mp4", "02.4K.SDR.mp4", "03.4K.SDR.mp4"}
    assert planned.filtered_out == 2  # 特典花絮 + 预告片
    assert planned.planned_existing == 0


def test_subdir_filter_recurses_only_into_existing_dirs():
    """首屏那个开关的真实语义：update_subdir=".*" + 目标已有目录 → 进去逐文件过滤。"""
    planned = _plan(update_subdir=".*", episode_start=1, episode_end=11, quality="4k")
    names = {f.share_name for f in planned.files}
    assert "11.4K.SDR.mp4" in names and "12.4K.SDR.mp4" not in names and "花絮A.mp4" not in names
    assert any(f.dest_path.endswith("/剧/合集/11.4K.SDR.mp4") for f in planned.files)


def test_task_spec_accepts_taskin_payload():
    """dry-run 端点靠 _task_spec 直接吃 TaskIn；这条把"12 个字段同名同型"这个假设钉死。"""
    from backend.schemas import TaskIn
    from backend.services.task_service import _task_spec

    body = TaskIn(taskname="剧", shareurl="https://pan.quark.cn/s/x", savepath="/剧", pattern="$TV",
                  update_subdir=".*", episode_start=5, episode_end=9, quality="4k", ignore_extension=True)
    spec = _task_spec(body)
    assert (spec.taskname, spec.shareurl, spec.savepath, spec.pattern, spec.update_subdir) == (
        "剧", "https://pan.quark.cn/s/x", "/剧", "$TV", ".*")
    assert (spec.episode_start, spec.episode_end, spec.quality, spec.ignore_extension) == (5, 9, "4k", True)


def test_dry_run_endpoint_fails_honestly_without_network():
    """端点的两条守卫路径必须离线可测：没有支持的驱动、没有可用账号，都如实报 ok=False。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as c:
        nope = c.post("/api/tasks/dry-run", json={
            "taskname": "剧", "shareurl": "https://pan.nosuch.example/s/abc", "savepath": "/剧"}).json()
        assert nope["ok"] is False and "驱动" in nope["message"]
        noacc = c.post("/api/tasks/dry-run", json={
            "taskname": "剧", "shareurl": "https://pan.quark.cn/s/abc", "savepath": "/剧",
            "account_id": 999999}).json()
        assert noacc["ok"] is False and "账号" in noacc["message"]
