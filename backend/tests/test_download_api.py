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
