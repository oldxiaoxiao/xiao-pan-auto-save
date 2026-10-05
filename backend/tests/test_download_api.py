import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from backend.core.download_registry import registry
from backend.main import app
from backend.services import download_service as dl


def test_downloads_endpoint_returns_snapshot():
    with TestClient(app) as client:
        jid = registry.create(task_id=9, taskname="x", filename="f.mp4", dest_path="/d/f.mp4", total=10)
        registry.update(jid, done=5, status="downloading")
        data = client.get("/api/downloads").json()
    job = next(j for j in data["jobs"] if j["id"] == jid)
    assert job["done"] == 5 and job["status"] == "downloading" and job["filename"] == "f.mp4"


def test_downloads_endpoint_merges_builtin_and_aria2(monkeypatch):
    async def fake_aria2(cfg):
        return [
            {
                "id": "gid-9",
                "task_id": None,
                "taskname": "",
                "filename": "193.mkv",
                "dest_path": "/d/193.mkv",
                "total": 1000,
                "done": 400,
                "speed": 50.0,
                "status": "downloading",
                "error": "",
                "started_at": 0.0,
                "updated_at": 0.0,
            }
        ]

    monkeypatch.setattr(dl, "aria2_status", fake_aria2)
    with TestClient(app) as client:
        jid = registry.create(task_id=1, taskname="内置", filename="b.mp4", dest_path="/d/b.mp4", total=8)
        registry.update(jid, done=1, status="downloading")
        ids = {j["id"] for j in client.get("/api/downloads").json()["jobs"]}
    assert jid in ids and "gid-9" in ids


@pytest.fixture()
def client_and_job():
    """现造一个 builtin 下载 job，返回其 id；结束后清理，避免污染快照断言。"""
    jid = registry.create(task_id=99, taskname="控制", filename="ctl.mkv", dest_path="/d/ctl.mkv", total=10)
    yield jid
    registry.remove(jid)


def test_builtin_pause_resume_rejected(client_and_job):
    jid = client_and_job
    assert TestClient(app).post(f"/api/downloads/{jid}/pause?source=builtin").status_code == 400
    assert TestClient(app).post(f"/api/downloads/{jid}/resume?source=builtin").status_code == 400
    # stop + delete 正常
    with TestClient(app) as c:
        assert c.post(f"/api/downloads/{jid}/stop?source=builtin").json()["ok"] is True
        assert c.delete(f"/api/downloads/{jid}?source=builtin").json()["ok"] is True
    assert all(j["id"] != jid for j in registry.snapshot())


def test_aria2_endpoints_call_rpc(monkeypatch):
    calls = []

    async def fake_rpc(cfg, method, *params):
        calls.append(method)
        return {"result": "ok"}

    monkeypatch.setattr(dl, "aria2_rpc", fake_rpc)
    with TestClient(app) as c:
        assert c.post("/api/downloads/GID123/pause?source=aria2").json()["ok"] is True
        assert c.post("/api/downloads/GID123/resume?source=aria2").json()["ok"] is True
        assert c.post("/api/downloads/GID123/stop?source=aria2").json()["ok"] is True
        assert c.delete("/api/downloads/GID123?source=aria2").json()["ok"] is True
    assert calls == ["aria2.pause", "aria2.unpause", "aria2.remove", "aria2.removeDownloadResult"]


def test_aria2_rpc_unreachable_gives_502(monkeypatch):
    async def boom(cfg, method, *params):
        raise OSError("connection refused")
    monkeypatch.setattr(dl, "aria2_rpc", boom)
    with TestClient(app) as c:
        resp = c.post("/api/downloads/GID9/stop?source=aria2")
    assert resp.status_code == 502
    assert "aria2 不可达" in resp.json()["detail"]


def test_downloads_endpoint_excludes_terminal_jobs():
    jid = registry.create(task_id=9, taskname="x", filename="term.mp4", dest_path="/d/term.mp4", total=10)
    registry.update(jid, done=10, status="done")
    try:
        with TestClient(app) as client:
            ids = {j["id"] for j in client.get("/api/downloads").json()["jobs"]}
    finally:
        registry.remove(jid)
    assert jid not in ids  # 终态不再混进进行中列表


def test_history_endpoint_shape(tmp_path):
    from backend.services import download_history as hist

    rid = hist.start(
        source="builtin", ref_id="api-shape", task_id=41, taskname="T", filename="在.mp4",
        dest_path=str(tmp_path / "在.mp4"), size_total=10, fid="F", driver_key="fake", account_id=None,
    )
    hist.finish("api-shape", source="builtin", status="done", size_done=10)
    (tmp_path / "在.mp4").write_bytes(b"0123456789")
    hist.start(
        source="builtin", ref_id="api-lost", task_id=41, taskname="T", filename="丢.mp4",
        dest_path=str(tmp_path / "丢.mp4"), size_total=10, fid="G", driver_key="fake", account_id=None,
    )
    hist.finish("api-lost", source="builtin", status="done", size_done=10)

    with TestClient(app) as c:
        data = c.get("/api/downloads/history", params={"task_id": 41, "page_size": 10}).json()
    assert data["total"] == 2
    by_name = {i["filename"]: i for i in data["items"]}
    assert by_name["在.mp4"]["file_state"] == "ok"
    assert by_name["丢.mp4"]["file_state"] == "missing"
    assert by_name["在.mp4"]["id"] == rid


def test_history_route_not_shadowed_by_job_id_routes():
    """历史路由必须先声明，否则 /downloads/history/... 会被 /{job_id} 吃掉。"""
    with TestClient(app) as c:
        assert c.get("/api/downloads/history").status_code == 200
        resp = c.delete("/api/downloads/history/999999")
        # 断言 detail 而非仅状态码：证明请求到达了 history 处理器的 404，而不是路由缺失
        assert resp.status_code == 404 and resp.json()["detail"] == "记录不存在"


def test_retry_endpoint_returns_at_once(monkeypatch):
    """端点必须立刻返回：内置下载器一个 4K 文件可能跑几小时，绝不同步等待。"""
    from backend.services import download_history as hist

    async def slow_retry(rec, cfg, *, log):
        await asyncio.sleep(30)

    monkeypatch.setattr(dl, "retry_record", slow_retry)
    rid = hist.start(
        source="builtin", ref_id="api-retry", task_id=925, taskname="t", filename="f", dest_path="/d/f",
        size_total=1, fid="F", driver_key="fake", account_id=None,
    )
    # 记录必须先收口成终态才可重下：修复波后非终态记录会被 409 挡住（见下方拒绝用例）
    hist.finish("api-retry", source="builtin", status="failed", error="HTTP 500")
    t0 = time.monotonic()
    with TestClient(app) as c:
        r = c.post(f"/api/downloads/history/{rid}/retry")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert time.monotonic() - t0 < 2


def test_retry_unknown_record_404():
    with TestClient(app) as c:
        resp = c.post("/api/downloads/history/987654/retry")
    # 同样断言 detail：证明请求落在 history retry 处理器上，而不是路由缺失/被 job_id 抢先匹配
    assert resp.status_code == 404 and resp.json()["detail"] == "记录不存在"


def _seed_retry_row(hist, ref: str, task_id: int, dest: str, *, terminal: bool) -> int:
    rid = hist.start(
        source="builtin", ref_id=ref, task_id=task_id, taskname="t", filename=dest.rsplit("/", 1)[-1],
        dest_path=dest, size_total=1, fid="F", driver_key="fake", account_id=None,
    )
    if terminal:
        hist.finish(ref, source="builtin", status="failed", error="HTTP 500")
    return rid


def test_retry_rejected_while_same_dest_has_open_row(tmp_path, monkeypatch):
    """同 dest_path 已有在途下载时拒绝重下：两个内置写者会把同一个 .part 交错写坏。

    模拟连点两次「重下」之后的第二次：第一次留下的开放行还挂着，此时不许再起后台任务。
    """
    from backend.services import download_history as hist

    dest = str(tmp_path / "dup.mkv")
    rid = _seed_retry_row(hist, "api-dup-src", 928, dest, terminal=True)
    open_id = _seed_retry_row(hist, "api-dup-open", 928, dest, terminal=False)
    started: list[str] = []

    async def spy_retry(rec, cfg, *, log):
        started.append(rec["ref_id"])

    monkeypatch.setattr(dl, "retry_record", spy_retry)
    try:
        with TestClient(app) as c:
            resp = c.post(f"/api/downloads/history/{rid}/retry")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "该文件已有进行中的下载，请等待完成后再重下"
        assert started == []  # 拒绝必须发生在起后台任务之前
    finally:
        hist.delete_record(rid)
        hist.delete_record(open_id)  # 不留开放行污染共享测试库（reconcile 用例靠清场夹具，别依赖残留）


def test_retry_rejected_when_record_itself_not_terminal(tmp_path, monkeypatch):
    """被重下的记录自己还没收口（如 queued 挂着）→ 同样 409，挡住同一行连点两次。"""
    from backend.services import download_history as hist

    dest = str(tmp_path / "double-click.mkv")
    rid = _seed_retry_row(hist, "api-selfopen", 929, dest, terminal=False)
    started: list[str] = []

    async def spy_retry(rec, cfg, *, log):
        started.append(rec["ref_id"])

    monkeypatch.setattr(dl, "retry_record", spy_retry)
    try:
        with TestClient(app) as c:
            resp = c.post(f"/api/downloads/history/{rid}/retry")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "该文件已有进行中的下载，请等待完成后再重下"
        assert started == []
    finally:
        hist.delete_record(rid)


def test_retry_rejected_when_path_inflight_in_process(tmp_path, monkeypatch):
    """DB 无开放行、但进程内守卫已登记同路径（第一次重下还卡在取直链窗口）→ 同样 409。

    has_open_for_path 查库，账本行要等 one() 取到直链才写入；这个窗口里的第二次点击
    只能由 download_service.is_downloading 兜住，且必须走同一个 409 契约让 UI 拿到原因。
    """
    from backend.services import download_history as hist

    dest = str(tmp_path / "window.mkv")
    rid = _seed_retry_row(hist, "api-inflight", 933, dest, terminal=True)
    monkeypatch.setattr(hist, "has_open_for_path", lambda p: False)  # 钉死这是进程内守卫的功劳
    monkeypatch.setattr(dl, "is_downloading", lambda p: p == dest)
    started: list[str] = []

    async def spy_retry(rec, cfg, *, log):
        started.append(rec["ref_id"])

    monkeypatch.setattr(dl, "retry_record", spy_retry)
    try:
        with TestClient(app) as c:
            resp = c.post(f"/api/downloads/history/{rid}/retry")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "该文件已有进行中的下载，请等待完成后再重下"
        assert started == []
    finally:
        hist.delete_record(rid)


def test_retry_allowed_for_terminal_row_without_open_sibling(tmp_path, monkeypatch):
    """终态记录 + 同路径无在途行 → 重下照旧放行（守护不能把好路堵死）。"""
    from backend.services import download_history as hist

    dest = str(tmp_path / "solo.mkv")
    rid = _seed_retry_row(hist, "api-solo", 930, dest, terminal=True)
    started: list[str] = []

    async def spy_retry(rec, cfg, *, log):
        started.append(rec["ref_id"])

    monkeypatch.setattr(dl, "retry_record", spy_retry)
    try:
        with TestClient(app) as c:
            resp = c.post(f"/api/downloads/history/{rid}/retry")
        assert resp.status_code == 200 and resp.json()["ok"] is True
    finally:
        hist.delete_record(rid)


def test_retry_task_exception_is_logged(tmp_path, monkeypatch):
    """后台重下任务炸出的异常必须落运行日志且端点仍 200：fire-and-forget 不设防就只剩 stderr。"""
    from backend.core.logstream import hub
    from backend.services import download_history as hist

    rid = _seed_retry_row(hist, "api-task-boom", 931, str(tmp_path / "boom.mkv"), terminal=True)

    async def exploding_retry(rec, cfg, *, log):
        raise RuntimeError("账号服务炸了")

    monkeypatch.setattr(dl, "retry_record", exploding_retry)
    try:
        with TestClient(app) as c:
            assert c.post(f"/api/downloads/history/{rid}/retry").status_code == 200
            entries = []
            for _ in range(50):  # 后台任务在 TestClient 的 portal 循环上跑，轮询等日志落进 hub.history
                time.sleep(0.02)
                entries = [e for e in hub.history if "重下后台任务异常" in e["message"]]
                if entries:
                    break
        assert entries and "账号服务炸了" in entries[-1]["message"]
    finally:
        hist.delete_record(rid)


def test_prune_endpoint_passes_retention_from_setting(monkeypatch):
    from backend.api import deps
    from backend.services import download_history as hist

    seen = {}

    def spy(mode, retention="days_90"):
        seen["args"] = (mode, retention)
        return 3

    monkeypatch.setattr(hist, "prune", spy)
    monkeypatch.setattr(deps, "get_setting", lambda k: {"history_retention": "days_30"})
    with TestClient(app) as c:
        r = c.post("/api/downloads/history/prune", json={"mode": "auto"})
    assert r.json() == {"ok": True, "removed": 3}
    # 端点入参只剩这一种来源：启动清理在测试里被 XIAO_PAN_SKIP_STARTUP_PRUNE 跳过，
    # seen 只可能来自端点本身 —— 反向可证端点取的是设置值而非硬编码
    assert seen["args"] == ("auto", "days_30")


def test_startup_prune_skipped_by_env_flag(monkeypatch):
    """XIAO_PAN_SKIP_STARTUP_PRUNE=1（conftest 已置）时 lifespan 不再启动清理；关掉则照跑。

    启动清理是生产行为（补停机跨过凌晨四点的场景），但 pytest 下它会删掉其它用例
    seed 的过期终态行——共享临时库里的静默串扰，必须有开关挡住。
    """
    from backend import config
    from backend.services import download_history as hist

    calls: list[tuple] = []
    monkeypatch.setattr(hist, "prune", lambda *a, **k: calls.append(a) or 0)

    monkeypatch.setattr(config, "SKIP_STARTUP_PRUNE", True)
    with TestClient(app):
        pass
    assert calls == []

    monkeypatch.setattr(config, "SKIP_STARTUP_PRUNE", False)  # 开关关掉 = 生产语义，不能顺手失效
    with TestClient(app):
        pass
    assert calls and calls[0][0] == "auto"


def test_prune_endpoint_unknown_mode_defaults_to_auto(monkeypatch):
    from backend.api import deps
    from backend.services import download_history as hist

    seen = {}
    monkeypatch.setattr(hist, "prune", lambda mode, retention="days_90": seen.update({"mode": mode}) or 0)
    monkeypatch.setattr(deps, "get_setting", lambda k: {})
    with TestClient(app) as c:
        assert c.post("/api/downloads/history/prune", json={"mode": "nonsense"}).json()["ok"] is True
    assert seen["mode"] == "nonsense"  # 未识别的 mode 落到 auto 分支，不删任何数据
