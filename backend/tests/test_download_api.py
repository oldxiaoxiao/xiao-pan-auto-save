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
