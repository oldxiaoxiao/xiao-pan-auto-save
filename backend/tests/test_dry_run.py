"""试跑（plan_only）与真实运行必须给出同一批结论；plan_only 不得产生任何写。"""

from __future__ import annotations

import asyncio

from backend.core.engine import TaskSpec, run_update_task
from backend.drivers.base import FsItem, SaveResult, ShareUnavailable
from backend.tests.test_task_service import BannedDriver, OkDriver

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


class NetworkDriver(PlanDriver):
    """继承 PlanDriver 只把 list_share 换成网络异常——钉端点 network 分类如实返回用的，不是第二份假盘。"""

    async def list_share(self, ref, path=""):
        raise ShareUnavailable("网盘连接超时")


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


# ---------- Finding 1：pattern="" 的同名撞车必须计入 planned_existing ----------


def test_existing_same_name_files_counted_without_pattern(monkeypatch):
    """pattern=""（最常见形态）+ 目标已有同名文件：第一次 is_exists 就命中，必须计「已存在跳过」。

    修前这一支无声跳过不计数，界面样例「已存在跳过 26 项」对这类任务恒为 0——就是说谎。
    同时断言这批文件不在「会转」集合里：计数与计划互斥，数字没虚增。
    """

    async def already_there(self, path):
        if path.endswith("/剧"):
            # 根里 5 个文件已全部转存在目标目录（同名未改名），「合集」目录也已在
            return [FsItem(fid="x1", name="合集", is_dir=True)] + [
                FsItem(fid=f"e{i}", name=n) for i, n in enumerate(_ROOT)
            ]
        return []

    monkeypatch.setattr(PlanDriver, "list_dir", already_there)
    planned = _plan()
    assert planned.planned_existing > 0, "同名未改名的已存在条目没计数，界面会显示「已存在跳过 0 项」"
    assert planned.planned_existing == 5, f"目标已有 5 个同名文件，应计 5：{planned.planned_existing}"
    assert _keyset(planned) == []  # 「已存在」与「会转」互斥：被计数的条目不许同时出现在计划里


def test_real_run_unchanged_when_same_name_files_exist(monkeypatch):
    """真实运行路径不受新计数影响：同一场景下写操作与转存集合和修前逐字节一致。"""

    async def already_there(self, path):
        if path.endswith("/剧"):
            return [FsItem(fid="x1", name="合集", is_dir=True)] + [
                FsItem(fid=f"e{i}", name=n) for i, n in enumerate(_ROOT)
            ]
        return []

    monkeypatch.setattr(PlanDriver, "list_dir", already_there)
    real = _real()
    # 修前实况：need_save 为空 → 只 ensure 过目标目录一次，一 save 都不发
    assert PlanDriver.calls == {"ensure_dir": 1}, f"真实运行写操作变了：{PlanDriver.calls}"
    assert _keyset(real) == [], f"已存在的文件不应被再转：{_keyset(real)}"
    assert real.status == "no_changes"


# ---------- Finding 2：端点 happy path / banned / network（设计 §6 三条，全部离线） ----------


def _seed_fake_account() -> int:
    """给端点测试建一条真库里的假账号（conftest 已把 DATA_DIR 指到临时目录），返回 id 供 finally 删。"""
    from backend.database import session_scope
    from backend.models import Account

    with session_scope() as s:
        acc = Account(driver_key="fake", name="试跑假号", enabled=True, can_save=True, cookie="x")
        s.add(acc)
        s.commit()
        s.refresh(acc)
        return int(acc.id)


def _drop_rows(*, account_id: int | None = None, task_ids: tuple[int, ...] = ()) -> None:
    """清掉端点测试自己建的行，遵循 test_task_run_mode._drop_account 的自建自清约定。"""
    from backend.database import session_scope
    from backend.models import Account, Task

    with session_scope() as s:
        if account_id is not None:
            row = s.get(Account, account_id)
            if row:
                s.delete(row)
        for tid in task_ids:
            trow = s.get(Task, tid)
            if trow:
                s.delete(trow)


def test_dry_run_endpoint_happy_path_writes_nothing(monkeypatch):
    """穿过守卫真进引擎的一次 POST：响应必须有真值，且任务表逐字段快照前后完全一致（设计 §6）。"""
    from fastapi.testclient import TestClient
    from sqlmodel import select

    from backend.core import router as core_router
    from backend.database import session_scope
    from backend.main import app
    from backend.models import Task

    monkeypatch.setattr(core_router, "route_driver", lambda url: PlanDriver)
    acc_id = _seed_fake_account()
    tid: int | None = None
    try:
        with session_scope() as s:
            t = Task(taskname="剧", shareurl="https://fake.example/s/1", savepath="/剧", account_id=acc_id)
            s.add(t)
            s.commit()
            s.refresh(t)
            tid = int(t.id)

        def _snapshot() -> dict:
            with session_scope() as s:
                return {
                    t.id: (t.last_run_at, t.retry_attempts, t.next_retry_at, t.disabled)
                    for t in s.exec(select(Task)).all()
                }

        before = _snapshot()
        with TestClient(app) as c:
            data = c.post("/api/tasks/dry-run", json={
                "taskname": "剧", "shareurl": "https://fake.example/s/1", "savepath": "/剧"}).json()
        # 防「测试自己骗自己」：这些真值只有穿过守卫、进了引擎才会存在
        assert data["ok"] is True and data["status"] == "updated", f"没走进引擎或状态不对：{data}"
        assert data["new_count"] == 5 and data["total_size"] > 0 and data["items"], f"响应没有真值：{data}"
        assert data["skipped_existing"] == 0 and data["filtered_out"] == 0
        after = _snapshot()
        assert after == before, f"dry-run 写库了：before={before} after={after}"
    finally:
        _drop_rows(account_id=acc_id, task_ids=((tid,) if tid is not None else ()))


def test_dry_run_endpoint_reports_banned_honestly(monkeypatch):
    """banned 必须原样穿过端点：ok=False、status="banned"、message 带出驱动给的原因，不许美化。"""
    from fastapi.testclient import TestClient

    from backend.core import router as core_router
    from backend.main import app

    monkeypatch.setattr(core_router, "route_driver", lambda url: BannedDriver)
    acc_id = _seed_fake_account()
    try:
        with TestClient(app) as c:
            data = c.post("/api/tasks/dry-run", json={
                "taskname": "剧", "shareurl": "https://fake.example/s/1", "savepath": "/剧"}).json()
        assert data["ok"] is False and data["status"] == "banned", data
        assert data["message"] == "分享已被取消", f"失效原因被改写：{data['message']}"
    finally:
        _drop_rows(account_id=acc_id)


def test_dry_run_endpoint_reports_network_honestly(monkeypatch):
    """network 分类同样如实：临时网络异常不许被粉饰成「没有新文件」。"""
    from fastapi.testclient import TestClient

    from backend.core import router as core_router
    from backend.main import app

    monkeypatch.setattr(core_router, "route_driver", lambda url: NetworkDriver)
    acc_id = _seed_fake_account()
    try:
        with TestClient(app) as c:
            data = c.post("/api/tasks/dry-run", json={
                "taskname": "剧", "shareurl": "https://fake.example/s/1", "savepath": "/剧"}).json()
        assert data["ok"] is False and data["status"] == "network", data
        assert data["message"] == "网盘连接超时", f"网络原因被改写：{data['message']}"
    finally:
        _drop_rows(account_id=acc_id)
