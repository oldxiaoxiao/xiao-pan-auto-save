"""执行形态（run_mode）测试：字段往返、非法值 400、对外接口默认值、调度层按形态驱动、一次性收口。"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete, select

from backend import main
from backend.core.engine import SavedFile, TaskRunResult
from backend.database import session_scope
from backend.main import app
from backend.models import RUN_MODES, Account, ExternalApiToken, Task
from backend.services import task_service as ts
from backend.services.task_service import DownloadCounts
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


def _drop_account(*ids: int) -> None:
    """清掉本模块测试自己 seed 的账号（Task 由 _drop 负责）。"""
    with session_scope() as s:
        for acc_id in ids:
            row = s.get(Account, acc_id)
            if row:
                s.delete(row)


def _spy_logs(monkeypatch) -> list[tuple[int | None, str, str]]:
    """包一层 hub.make_logger，按 (task_id, level, msg) 收全运行日志。

    返回的列表跨测试共享同一 DB，故断言必须按本测试建出来的 task id 过滤。
    """
    collected: list[tuple[int | None, str, str]] = []
    real_make_logger = ts.hub.make_logger

    def spy_make_logger(run_id="", task_id=None):
        inner = real_make_logger(run_id, task_id)

        def spy(level, msg, **kw):
            collected.append((task_id, level, msg))
            inner(level, msg, **kw)

        return spy

    monkeypatch.setattr(ts.hub, "make_logger", spy_make_logger)
    return collected


# ---------- 一次性收口：新增 + 下载全成功才算跑完 ----------


def _collect(sink: list[tuple[str, str]]):
    """把 tlog 调用收进列表，便于断言日志级别与文案。"""

    def tlog(level, message):
        sink.append((level, message))

    return tlog


def _once_task(**kw) -> Task:
    defaults = dict(taskname="一次性", shareurl="https://pan.quark.cn/s/x", savepath="/x", run_mode="once")
    defaults.update(kw)
    return Task(**defaults)


def _updated_result() -> TaskRunResult:
    return TaskRunResult(
        status="updated",
        files=[SavedFile(share_name="1.mp4", final_name="1.mp4", new_fid="f1", dest_path="/x/1.mp4")],
    )


@pytest.mark.parametrize(
    "auto_download,counts,status,want_done,want_reason_part",
    [
        (False, DownloadCounts(), "updated", True, "已获取新增资源"),
        (True, DownloadCounts(executed=True, attempted=2, ok=2), "updated", True, "已获取新增资源"),
        (True, DownloadCounts(executed=True, attempted=1, ok=1), "updated", True, "已获取新增资源"),
        (True, DownloadCounts(executed=True, attempted=2, ok=1, failed=1), "updated", False,
         "1 项下载失败（可到下载页逐条重下）"),
        (True, DownloadCounts(executed=False), "updated", False, "下载未实际执行"),
        (True, DownloadCounts(executed=True, attempted=0), "updated", False, "下载未实际执行"),
        (False, DownloadCounts(), "no_changes", False, "本次没有新增资源"),
        (True, DownloadCounts(executed=True, attempted=1, ok=1), "no_changes", False, "本次没有新增资源"),
        (False, DownloadCounts(), "failed", False, "转存未成功"),
    ],
)
def test_once_verdict(auto_download, counts, status, want_done, want_reason_part):
    task = _once_task(auto_download=auto_download)
    result = TaskRunResult(status=status) if status != "updated" else _updated_result()
    done, reason = ts._once_verdict(task, result, counts)
    assert done is want_done
    assert want_reason_part in reason


def test_once_verdict_skips_disabled_task_for_idempotency():
    """已停用（含此前自动完成）的一次性任务再手动跑一次，不重复收口、不重复通知。"""
    task = _once_task(auto_download=False, disabled=True)
    done, reason = ts._once_verdict(task, _updated_result(), DownloadCounts())
    assert not done and "停用" in reason


def test_once_verdict_ignores_non_once_tasks():
    task = _once_task(run_mode="follow", auto_download=False)
    done, reason = ts._once_verdict(task, _updated_result(), DownloadCounts())
    assert not done and reason == "非一次性任务"


def test_once_verdict_normalizes_legacy_blank_run_mode():
    """老库升级出来的 run_mode='' 归一化为 follow：不能被一次性逻辑误收口。"""
    task = _once_task(run_mode="", auto_download=False)
    done, reason = ts._once_verdict(task, _updated_result(), DownloadCounts())
    assert done is False and reason == "非一次性任务"


@pytest.mark.asyncio
async def test_run_once_auto_disables_after_success(monkeypatch):
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=False)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        with session_scope() as s:
            row = s.get(Task, tid)
            assert row.disabled is True
            # 停用写的是第二个事务：不能把上一个事务刚落好的 last_run_at 覆盖成空
            assert row.last_run_at is not None
    finally:
        _drop(tid)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_run_once_auto_disables_when_all_downloads_skipped_as_existing(monkeypatch):
    """aria2/本地下载全部「✅ 跳过（已存在）」= 文件已在盘上 = 成功，一次性任务照样收口。"""
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=True)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    from backend.services import download_service

    async def fake_download(*a, **k):
        return ["✅ 跳过（已存在）1.mp4", "✅ 跳过（已存在）2.mp4"]

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    monkeypatch.setattr(download_service, "download_task_files", fake_download)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        with session_scope() as s:
            assert s.get(Task, tid).disabled is True
    finally:
        _drop(tid)


@pytest.mark.asyncio
async def test_run_once_stays_enabled_when_download_failed(monkeypatch):
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=True)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    from backend.services import download_service

    async def fake_download(*a, **k):
        return ["✅ 1.mp4（1.0MB）", "❌ 2.mp4: HTTP 500"]

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    monkeypatch.setattr(download_service, "download_task_files", fake_download)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        with session_scope() as s:
            assert s.get(Task, tid).disabled is False
    finally:
        _drop(tid)


@pytest.mark.asyncio
async def test_run_once_stays_enabled_when_driver_cannot_download(monkeypatch):
    """开了下载但驱动不支持 → 下载未实际执行 → 不能算完成（防"看着完成了其实没下"）。"""
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=True)
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)  # 只有 rename 能力

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        with session_scope() as s:
            assert s.get(Task, tid).disabled is False
    finally:
        _drop(tid)


@pytest.mark.asyncio
async def test_run_once_stays_enabled_when_no_changes(monkeypatch):
    """分享还没放资源：保持启用，且不给非一次性任务刷"未完成"告警。"""
    acc_id = _seed_account()
    once_id = _make_task("once", account_id=acc_id, auto_download=False)
    follow_id = _make_task("follow", account_id=acc_id, auto_download=False)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    logs = _spy_logs(monkeypatch)
    try:
        await ts.run_tasks(task_ids=[once_id, follow_id], trigger="manual")
        with session_scope() as s:
            assert s.get(Task, once_id).disabled is False
            assert s.get(Task, follow_id).disabled is False
        # 「未完成」告警只对一次性任务说：判定为"非一次性任务"的运行不得为该条刷 warn
        mine = [(level, msg) for task_id, level, msg in logs if task_id == follow_id]
        assert not [msg for level, msg in mine if level == "warn" and "未完成" in msg]
    finally:
        _drop(once_id, follow_id)


def test_settle_once_survives_task_deleted_mid_run():
    """收口写库时任务已被删除：session.get 返回 None，必须安静跳过而不是抛错或写出半行。"""
    task = _once_task(auto_download=False)
    task.id = 999999  # 不存在的 id
    calls: list[tuple[str, str]] = []

    def tlog(level, msg):
        calls.append((level, msg))

    ts._settle_once(task, _updated_result(), DownloadCounts(), tlog)

    with session_scope() as s:
        assert s.get(Task, 999999) is None
    assert calls == []


@pytest.mark.asyncio
async def test_run_once_is_idempotent_on_second_manual_run(monkeypatch):
    """已完成（自动停用）的一次性任务再手动跑一次：不重复写、不重复通知收口文案。"""
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=False)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    logs = _spy_logs(monkeypatch)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        mine = [msg for task_id, _, msg in logs if task_id == tid]
        first = [msg for msg in mine if "一次性任务已完成" in msg]
        assert len(first) == 1
        logs.clear()

        await ts.run_tasks(task_ids=[tid], trigger="manual")
        mine = [msg for task_id, _, msg in logs if task_id == tid]
        assert [msg for msg in mine if "一次性任务已完成" in msg] == []
        assert [msg for msg in mine if "未完成" in msg] == []
        with session_scope() as s:
            assert s.get(Task, tid).disabled is True
    finally:
        _drop(tid)


def test_download_for_task_returns_counts_and_keeps_notify_line(monkeypatch):
    """结构化计数：通知文案逐字不变，计数供一次性判定复用。"""
    from backend.services import download_service

    acc_id = _seed_account()
    tid = _make_task("follow", account_id=acc_id, auto_download=True)
    task = _reload_task(tid)
    driver = DownloadOkDriver(cookie="x", proxy=None, index=0)
    result = TaskRunResult(
        status="updated",
        files=[SavedFile(share_name="1.mp4", final_name="1.mp4", new_fid="f1", dest_path="/x/1.mp4")],
    )

    async def fake_download(*a, **k):
        return ["✅ 1.mp4（1.0MB）", "❌ 2.mp4: HTTP 500"]

    lines: list[str] = []
    logs: list[tuple[str, str]] = []
    monkeypatch.setattr(download_service, "download_task_files", fake_download)
    try:
        counts = asyncio.run(ts._download_for_task(driver, task, result, {}, lines, _collect(logs)))
        assert (counts.executed, counts.attempted, counts.ok, counts.failed) == (True, 2, 1, 1)
        assert lines == ["📥《形态follow》本地下载 1/2：\n✅ 1.mp4（1.0MB）\n❌ 2.mp4: HTTP 500"]

        # 驱动不支持下载：executed=False，且不产生通知行
        unsupported = OkDriver(cookie="x", proxy=None, index=0)
        lines2: list[str] = []
        counts2 = asyncio.run(ts._download_for_task(unsupported, task, result, {}, lines2, _collect(logs)))
        assert (counts2.executed, counts2.attempted, counts2.ok, counts2.failed) == (False, 0, 0, 0)
        assert lines2 == []
        assert any(level == "warn" and "不支持下载" in msg for level, msg in logs)
    finally:
        _drop(tid)


# ---------- 收口提示补全：没拿到新增 / 转存失败也要告诉用户「还在待执行」 ----------


@pytest.mark.asyncio
async def test_run_once_no_changes_logs_pending_hint(monkeypatch):
    """分享还没放资源（一次性任务最常见的场景）：保持启用，且必须在任务日志里说明仍在待执行。"""
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=False)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    logs = _spy_logs(monkeypatch)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        with session_scope() as s:
            assert s.get(Task, tid).disabled is False
        mine = [msg for task_id, _, msg in logs if task_id == tid]
        assert any("本次没有新增资源" in m and "保持待执行" in m for m in mine), mine
    finally:
        _drop(tid)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_run_once_transfer_failed_logs_pending_hint(monkeypatch):
    """转存失败：保持启用并写明「转存未成功」，用户才知道一次性任务没白跑也没跑完。"""
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=False)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return TaskRunResult(status="failed", message="转存被风控")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    logs = _spy_logs(monkeypatch)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        with session_scope() as s:
            assert s.get(Task, tid).disabled is False
        mine = [msg for task_id, _, msg in logs if task_id == tid]
        assert any("转存未成功" in m for m in mine), mine
    finally:
        _drop(tid)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_run_follow_no_changes_emits_no_once_line(monkeypatch):
    """收口提示只对一次性任务说：follow 任务 no_changes 时一行「一次性任务」文案都不能有。"""
    acc_id = _seed_account()
    tid = _make_task("follow", account_id=acc_id, auto_download=False)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    logs = _spy_logs(monkeypatch)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        mine = [msg for task_id, _, msg in logs if task_id == tid]
        assert [m for m in mine if "一次性任务" in m] == []
        assert any("没有新的转存" in m for m in mine)  # 原有文案不受影响
    finally:
        _drop(tid)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_run_once_disabled_row_second_run_stays_silent(monkeypatch):
    """已完成（停用）的一次性任务再手动点一次：既不重复报完成，也不报未完成。"""
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=False, disabled=True)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    logs = _spy_logs(monkeypatch)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        mine = [msg for task_id, _, msg in logs if task_id == tid]
        assert [m for m in mine if "已完成并自动停用" in m] == []
        assert [m for m in mine if "未完成" in m] == []
        with session_scope() as s:
            assert s.get(Task, tid).disabled is True
    finally:
        _drop(tid)
        _drop_account(acc_id)


# ---------- 批量「立即运行」跳过停用任务（行内「▶ 运行」例外） ----------


@pytest.mark.asyncio
async def test_bulk_run_skips_disabled_tasks(monkeypatch):
    """全局「立即运行」=「暂停就该真暂停」：停用行不参与，未停用行照跑并计数。"""
    acc_id = _seed_account()
    dead = _make_task("follow", account_id=acc_id, disabled=True)
    live = _make_task("manual", account_id=acc_id)  # 仅手动 + 未停用 → 批量点应参与
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    logs = _spy_logs(monkeypatch)
    try:
        summary = await ts.run_tasks(trigger="manual")  # 全局「立即运行」
        assert summary["disabled_skipped"] == 1
        # 本模块的 autouse 夹具每个用例前后都清空 Task/Account/ExternalApiToken，
        # 所以这里 total==2 只数到本用例自己建的 2 行，不是全表断言；
        # 且 total 刻意包含停用行（驱动数看 driven），留着它钉住这个语义。
        assert summary["total"] == 2
        assert ran == ["形态manual"]  # 停用那行一次都没进引擎
        dead_logs = [msg for task_id, _, msg in logs if task_id == dead]
        assert any("已停用" in m and "本次不驱动" in m for m in dead_logs), dead_logs
        assert not [m for m in dead_logs if "一次性任务" in m]
    finally:
        _drop(dead, live)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_single_run_of_disabled_task_still_works(monkeypatch):
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, disabled=True)
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)
    called: list[int] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        called.append(1)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        assert called == [1]  # 行内「运行」是明确的手工意图，不被停用规则挡
    finally:
        _drop(tid)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_scheduled_sweep_of_disabled_task_reports_stopped_reason(monkeypatch):
    """带有效 schedule 的停用任务被定时扫到时，原因必须写「停用」而不是「已配置独立调度」。"""
    acc_id = _seed_account()
    tid = _make_task("follow", account_id=acc_id, disabled=True, schedule="interval:30")
    monkeypatch.setattr(ts, "route_driver", lambda url: OkDriver)
    ran: list[int] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(1)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    logs = _spy_logs(monkeypatch)
    try:
        summary = await ts.run_tasks(trigger="scheduled")
        assert summary["disabled_skipped"] == 1
        assert ran == []
        mine = [msg for task_id, _, msg in logs if task_id == tid]
        assert any("已停用" in m for m in mine), mine
        assert not [m for m in mine if "独立调度" in m]
    finally:
        _drop(tid)
        _drop_account(acc_id)


# ---------- 运行汇总契约：收口写库不许拖垮整批、停用跳过要说给用户、算术要闭合 ----------


def _spy_push(monkeypatch) -> list[tuple[str, str]]:
    """收全交给 _push 的 (标题, 正文)：通知文案是对用户的契约，断言正文而不是断言渠道实现。"""
    pushed: list[tuple[str, str]] = []

    async def spy_push(title, content, push_config, settings, log):
        pushed.append((title, content))

    monkeypatch.setattr(ts, "_push", spy_push)
    return pushed


@contextmanager
def _scope_that_fails_on_disable_writes():
    """只让「把行置为 disabled」这一次落库抛错——模拟 SQLite 没有 WAL/busy_timeout 时的写锁失败，
    其余读写（load_tasks、last_run_at、停用复查）照旧，好把爆炸半径限制在一次性收口这一步。"""
    with session_scope() as session:
        real_add = session.add

        def add(obj):
            if isinstance(obj, Task) and getattr(obj, "disabled", False):
                raise RuntimeError("database is locked")
            return real_add(obj)

        session.add = add
        yield session


@pytest.mark.asyncio
async def test_settle_write_failure_does_not_kill_the_run_batch(monkeypatch):
    """账本式写库（一次性收口停用）失败是旁路观测：本批余下任务照跑、已产出的通知文案照发。"""
    acc_id = _seed_account()
    once_id = _make_task("once", account_id=acc_id, auto_download=False)
    follow_id = _make_task("follow", account_id=acc_id, auto_download=False)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    monkeypatch.setattr(ts, "session_scope", _scope_that_fails_on_disable_writes)
    pushed = _spy_push(monkeypatch)
    logs = _spy_logs(monkeypatch)
    try:
        await ts.run_tasks(task_ids=[once_id, follow_id], trigger="manual")
        mine = [msg for task_id, _, msg in logs if task_id == once_id]
        assert any("一次性收口失败（不影响运行）" in m for m in mine), mine
        assert "形态follow" in ran  # (a) 收口炸了之后的那个任务照样进引擎
        assert len(pushed) == 1  # (b) 已经产出的通知文案没被一起带崩
        assert "《形态once》" in pushed[0][1] and "《形态follow》" in pushed[0][1]
        with session_scope() as s:
            assert s.get(Task, once_id).disabled is False  # 收口没写成，行仍保持待执行
    finally:
        _drop(once_id, follow_id)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_settle_once_gives_up_when_mode_switched_back_mid_run(monkeypatch):
    """转存 + 大下载要跑几分钟：期间用户把这行改回定时追更，收口必须放弃停用，
    否则它会变成「已停用 + 仍挂着定时作业」的矛盾态（apply_task_schedule 在改回时已重新注册）。"""
    acc_id = _seed_account()
    tid = _make_task("once", account_id=acc_id, auto_download=False)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        with session_scope() as s:  # 运行途中改回 follow
            row = s.get(Task, tid)
            row.run_mode = "follow"
            s.add(row)
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    logs = _spy_logs(monkeypatch)
    try:
        await ts.run_tasks(task_ids=[tid], trigger="manual")
        mine = [msg for task_id, _, msg in logs if task_id == tid]
        assert not [m for m in mine if "已完成并自动停用" in m], mine
        with session_scope() as s:
            row = s.get(Task, tid)
            assert row.disabled is False
            assert row.run_mode == "follow"
    finally:
        _drop(tid)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_bulk_run_notification_states_disabled_skipped(monkeypatch):
    """spec 4.4：通知要写明这次跳过了几个已停用任务，否则用户会以为漏跑。"""
    acc_id = _seed_account()
    dead = _make_task("follow", account_id=acc_id, disabled=True)
    live = _make_task("follow", account_id=acc_id)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    pushed = _spy_push(monkeypatch)
    try:
        summary = await ts.run_tasks(trigger="manual")
        assert summary["disabled_skipped"] == 1
        assert len(pushed) == 1
        assert "⏸️ 本次跳过 1 个已停用任务" in pushed[0][1]
    finally:
        _drop(dead, live)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_notification_omits_disabled_line_when_nothing_skipped(monkeypatch):
    """一个停用行都没跳过时不许出现「跳过 0 个已停用任务」这种噪音。"""
    acc_id = _seed_account()
    live = _make_task("follow", account_id=acc_id)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    pushed = _spy_push(monkeypatch)
    try:
        summary = await ts.run_tasks(trigger="manual")
        assert summary["disabled_skipped"] == 0
        assert len(pushed) == 1
        assert "已停用任务" not in pushed[0][1]
    finally:
        _drop(live)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_summary_driven_closes_the_arithmetic(monkeypatch):
    """混合批：total == driven + skipped + disabled_skipped。
    这是前端 RunSummary 的显示契约（「实际运行」不能再和「跳过」重复计数），必须被测试钉住。"""
    acc_id = _seed_account()
    driven_id = _make_task("follow", account_id=acc_id)  # 无独立调度 → 定时扫驱动
    once_id = _make_task("once", account_id=acc_id)  # 按形态跳过
    dead_id = _make_task("follow", account_id=acc_id, disabled=True)  # 按停用跳过
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    try:
        summary = await ts.run_tasks(trigger="scheduled")
        assert summary["driven"] == 1
        assert summary["skipped"] == 1
        assert summary["disabled_skipped"] == 1
        assert summary["total"] == summary["driven"] + summary["skipped"] + summary["disabled_skipped"]
        assert len(ran) == 1  # 只有那一个未停用 follow 行进了引擎
    finally:
        _drop(driven_id, once_id, dead_id)
        _drop_account(acc_id)


@pytest.mark.asyncio
async def test_empty_task_ids_list_is_treated_as_global_sweep(monkeypatch):
    """task_ids=[] 与 None 同义（load_tasks 也按真值判断）：不能悄悄变成「连停用行一起跑」。"""
    acc_id = _seed_account()
    dead = _make_task("follow", account_id=acc_id, disabled=True)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return TaskRunResult(status="no_changes")

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    try:
        summary = await ts.run_tasks(task_ids=[], trigger="manual")
        assert ran == []
        assert summary["disabled_skipped"] == 1
    finally:
        _drop(dead)
        _drop_account(acc_id)


# ---------- 转存运行时间落库失败：不许拖垮整批（0ed6400 同类问题的 last_run_at 版） ----------


def _scope_that_fails_first_last_run_write():
    """只让第一次写 last_run_at 的落库抛错——模拟 SQLite 没有 WAL/busy_timeout 时的写锁失败。
    其余读写（load_tasks、_pick_account、第二个任务的落库）照旧，把爆炸半径限制在这一步。"""

    def make_scope():
        state = {"fired": False}

        @contextmanager
        def scope():
            with session_scope() as session:
                real_add = session.add

                def add(obj):
                    if (
                        not state["fired"]
                        and isinstance(obj, Task)
                        and getattr(obj, "last_run_at", None) is not None
                    ):
                        state["fired"] = True
                        raise RuntimeError("database is locked")
                    return real_add(obj)

                session.add = add
                yield session

        return scope()

    return make_scope


@pytest.mark.asyncio
async def test_last_run_write_failure_does_not_kill_the_run_batch(monkeypatch):
    """last_run_at 落库失败只许 warn：转存已成功的结果、本批余下任务、聚合通知都不许被带崩。"""
    acc_id = _seed_account()
    first_id = _make_task("follow", account_id=acc_id, auto_download=False)
    second_id = _make_task("manual", account_id=acc_id, auto_download=False)
    monkeypatch.setattr(ts, "route_driver", lambda url: DownloadOkDriver)
    ran: list[str] = []

    async def fake_run_update(driver, spec, magic_regex=None, log=None):
        ran.append(spec.taskname)
        return _updated_result()

    monkeypatch.setattr(ts, "run_update_task", fake_run_update)
    monkeypatch.setattr(ts, "session_scope", _scope_that_fails_first_last_run_write())
    pushed = _spy_push(monkeypatch)
    logs = _spy_logs(monkeypatch)
    try:
        await ts.run_tasks(task_ids=[first_id, second_id], trigger="manual")
        # (a) 第一个任务落库炸了，第二个任务照样进引擎
        assert ran == ["形态follow", "形态manual"]
        # (b) 已产出的通知文案没被一起带崩：两个任务的成功转存都要推出去
        assert len(pushed) == 1
        assert "《形态follow》" in pushed[0][1] and "《形态manual》" in pushed[0][1]
        # (c) 失败本身要在第一个任务的日志里以 warn 说明
        mine = [(level, msg) for task_id, level, msg in logs if task_id == first_id]
        assert any(
            level == "warn" and "运行时间落库失败（不影响本次结果）" in msg and "database is locked" in msg
            for level, msg in mine
        ), mine
    finally:
        _drop(first_id, second_id)
        _drop_account(acc_id)
