"""执行形态（run_mode）测试：字段往返、非法值 400、对外接口默认值。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete

from backend.database import session_scope
from backend.main import app
from backend.models import RUN_MODES, Account, ExternalApiToken, Task


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
