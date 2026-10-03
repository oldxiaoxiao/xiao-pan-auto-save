from fastapi.testclient import TestClient

from backend.core.download_registry import registry
from backend.main import app


def test_downloads_endpoint_returns_snapshot():
    with TestClient(app) as client:
        jid = registry.create(task_id=9, taskname="x", filename="f.mp4", dest_path="/d/f.mp4", total=10)
        registry.update(jid, done=5, status="downloading")
        data = client.get("/api/downloads").json()
    job = next(j for j in data["jobs"] if j["id"] == jid)
    assert job["done"] == 5 and job["status"] == "downloading" and job["filename"] == "f.mp4"
