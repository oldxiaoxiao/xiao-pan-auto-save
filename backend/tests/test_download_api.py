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
    from backend.core.download_registry import DownloadRegistry as _R  # noqa: F401

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
        assert c.delete("/api/downloads/history/999999").status_code == 404
