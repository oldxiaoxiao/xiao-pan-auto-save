"""执行形态（run_mode）测试：字段往返、非法值 400、对外接口默认值、调度层按形态驱动。"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete, select

from backend import main
from backend.core.engine import TaskRunResult
from backend.database import session_scope
from backend.main import app
from backend.models import RUN_MODES, Account, ExternalApiToken, Task
from backend.services import task_service as ts
from backend.tests.test_task_service import OkDriver


class DownloadOkDriver(OkDriver):
    """带下载能力的假盘：test_task_service.OkDriver 的 capability 只有 rename。"""

    capability = {"rename", "download"}
    name = "假盘（可下载）"

    async def get_download_urls(self, fids):
        return (
            [{"fid": f, "file_name": "1.mp4", "size": 10, "download_url": f"http://d/{f}"} for f in fids],
            "ck=1",
        )


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_db():
    """本模块自己建自己清，避免与同库其他模块互相干扰。"""
    for model in (Task, Account, ExternalApiToken):
        with session_scope() as s:
            s.exec(delete(model))
    yield
    for model in (Task, Account, ExternalApiToken):
        with session_scope() as s:
            s.exec(delete(model))


def _body(**extra):
    return {"taskname": "剧集", "shareurl": "https://pan.quark.cn/s/abc", "savepath": "/剧", **extra}


def test_default_run_mode_is_follow(client):
    created = client.post("/api/tasks", json=_body()).json()
    assert created["run_mode"] == "follow"


@pytest.mark.parametrize("mode", list(RUN_MODES))
def test_run_mode_roundtrip(client, mode):
    created = client.post("/api/tasks", json=_body(run_mode=mode)).json()
    assert created["run_mode"] == mode
    assert client.get("/api/tasks").json()[0]["run_mode"] == mode
    updated = client.put(f"/api/tasks/{created['id']}", json={**created, "run_mode": "once"}).json()
    assert updated["run_mode"] == "once"


def test_invalid_run_mode_rejected_on_create(client):
    resp = client.post("/api/tasks", json=_body(run_mode="sometimes"))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "执行方式只能是 follow / manual / once"


def test_invalid_run_mode_rejected_on_update(client):
    created = client.post("/api/tasks", json=_body()).json()
    resp = client.put(f"/api/tasks/{created['id']}", json={**created, "run_mode": "nope"})
    assert resp.status_code == 400


def test_payload_without_run_mode_stays_follow(client):
    """老调用方不传 run_mode 不能报错，按 follow 处理。"""
    created = client.post("/api/tasks", json=_body()).json()
    payload = {k: v for k, v in created.items() if k != "run_mode"}
    assert client.put(f"/api/tasks/{created['id']}", json=payload).json()["run_mode"] == "follow"


def test_external_add_task_defaults_to_follow_and_rejects_bad_value(client):
    token = client.post("/api/tokens", json={"name": "油猴"}).json()["token"]
    ok = client.post(
        f"/api/add_task?token={token}", json={"taskname": "a", "shareurl": "https://pan.quark.cn/s/a", "savepath": "/a"}
    ).json()
    assert ok["success"] is True
    assert client.get("/api/tasks").json()[0]["run_mode"] == "follow"

    bad = client.post(
        f"/api/add_task?token={token}",
        json={"taskname": "b", "shareurl": "https://pan.quark.cn/s/b", "savepath": "/b", "run_mode": "weekly"},
    ).json()
    assert bad == {"success": False, "code": 2, "message": "执行方式非法: weekly"}


# ---------- 调度层：非 follow 既不注册任务级作业，也不参与全局 sweep ----------


def _make_task(mode: str, **extra) -> int:
    with session_scope() as s:
        t = Task(
            taskname=f"形态{mode}", shareurl="https://pan.quark.cn/s/x", savepath="/x", run_mode=mode, **extra
        )
        s.add(t)
        s.commit()
        s.refresh(t)
        return int(t.id)


def _reload_task(tid: int) -> Task:
    with session_scope() as s:
        return s.get(Task, tid)


def _drop(*ids: int) -> None:
    from backend.main import scheduler

    with session_scope() as s:
        for tid in ids:
            row = s.get(Task, tid)
            if row:
                s.delete(row)
    for tid in ids:
        scheduler.unschedule_task(tid)


def _seed_account() -> int:
    with session_scope() as s:
        acc = Account(driver_key="fake", cookie="x", enabled=True, can_save=True, name="主号")
        s.add(acc)
        s.commit()
        s.refresh(acc)
        return int(acc.id)


def test_manual_and_once_do_not_register_task_job():
    from backend.main import scheduler

    ids = []
    try:
        for mode in ("manual", "once"):
            tid = _make_task(mode, schedule="interval:30")
            ids.append(tid)
            main.apply_task_schedule(_reload_task(tid))
            assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is None
    finally:
        _drop(*ids)


def test_follow_registers_and_switching_to_manual_cancels():
    from backend.main import scheduler

    tid = _make_task("follow", schedule="interval:30")
    try:
        main.apply_task_schedule(_reload_task(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is not None
        with session_scope() as s:
            row = s.get(Task, tid)
            row.run_mode = "manual"
            s.add(row)
        main.apply_task_schedule(_reload_task(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is None
    finally:
        _drop(tid)


def test_run_one_task_unschedules_itself_when_mode_changed():
    """定时器触发时若形态已改成非 follow，作业必须自我撤销，不能再驱动。"""
    from backend.main import scheduler

    tid = _make_task("follow", schedule="interval:30")
    try:
        main.apply_task_schedule(_reload_task(tid))
        with session_scope() as s:
            row = s.get(Task, tid)
            row.run_mode = "once"
            s.add(row)
        asyncio.run(main._run_one_task(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is None
    finally:
        _drop(tid)


@pytest.mark.asyncio
async def test_scheduled_sweep_skips_non_follow_modes(monkeypatch):
    acc_id = _seed_account()
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)
    ran: list[int] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(1)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    ids = [_make_task(m, account_id=acc_id) for m in ("manual", "once")]
    try:
        summary = await ts.run_tasks(trigger="scheduled")
        assert summary["skipped"] == 2 and summary["total"] == 2
        assert ran == []  # 一个都没被驱动
    finally:
        _drop(*ids)
        with session_scope() as s:
            row = s.get(Account, acc_id)
            if row:
                s.delete(row)


@pytest.mark.asyncio
async def test_manual_trigger_still_runs_non_follow_modes(monkeypatch):
    acc_id = _seed_account()
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    tid = _make_task("manual", account_id=acc_id)
    try:
        summary = await ts.run_tasks(task_ids=[tid], trigger="manual")
        assert ran and summary["failed"] == 0
    finally:
        _drop(tid)
        with session_scope() as s:
            row = s.get(Account, acc_id)
            if row:
                s.delete(row)


def test_run_mode_of_normalizes_empty_and_unknown():
    from backend.models import run_mode_of

    assert run_mode_of(Task(taskname="老", shareurl="u", savepath="/a", run_mode="")) == "follow"
    assert run_mode_of(Task(taskname="怪", shareurl="u", savepath="/a", run_mode="nonsense")) == "follow"
    assert run_mode_of(Task(taskname="手动", shareurl="u", savepath="/a", run_mode="manual")) == "manual"


def test_legacy_blank_row_still_gets_a_timer():
    """模拟升级后的存量行（run_mode=''）：定时器必须照旧注册，不能静默停更。"""
    from backend.main import scheduler

    tid = _make_task("follow", schedule="interval:30")
    with session_scope() as s:  # 直写成空串，复刻 _auto_add_columns 补列后的真实数据形状
        raw = s.exec(select(Task).where(Task.id == tid)).first()
        raw.run_mode = ""
        s.add(raw)
    try:
        main.apply_task_schedule(_reload_task(tid))
        assert scheduler.scheduler.get_job(f"xiao_pan_task_{tid}") is not None
    finally:
        _drop(tid)


def test_legacy_blank_row_projects_as_follow_in_api(client):
    """同一份归一化也作用于 API 回显：前端与油猴列表拿不到 ''。"""
    created = client.post("/api/tasks", json=_body()).json()
    tid = created["id"]
    with session_scope() as s:  # 直写成空串，复刻 _auto_add_columns 补列后的真实数据形状
        raw = s.exec(select(Task).where(Task.id == tid)).first()
        raw.run_mode = ""
        s.add(raw)
    try:
        assert _reload_task(tid).run_mode == ""  # 库里确实是空串，下面回显的才是归一化值
        assert client.get("/api/tasks").json()[0]["run_mode"] == "follow"
    finally:
        _drop(tid)
