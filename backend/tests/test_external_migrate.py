"""对外 API 与迁移导入测试。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete

from backend.database import session_scope
from backend.main import app
from backend.models import Account, ExternalApiToken, Task

OLD_CONFIG = {
    "cookie": ["CK_ONE=1; __uid=a", "CK_TWO=2"],
    "crontab": "15 10 * * *",
    "push_config": {"PUSH_KEY": "SCT123", "CONSOLE": True},
    "magic_regex": {"$DEMO": {"pattern": "x", "replace": "y"}},
    "tasklist": [
        {
            "taskname": "旧任务A",
            "shareurl": "https://pan.quark.cn/s/aaa",
            "savepath": "/动漫/A",
            "pattern": "$TV",
            "replace": "",
            "ignore_extension": True,
            "update_subdir": "4K",
            "update_subdir_resave_mode": True,
            "runweek": [1, 3],
            "enddate": "2026-12-31",
            "addition": {"some_plugin": {"on": True}},
        },
        {"taskname": "缺字段的", "shareurl": "x"},
    ],
}


@pytest.fixture
def client():
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(Account))
        s.exec(delete(ExternalApiToken))
    with TestClient(app) as c:
        yield c
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(Account))
        s.exec(delete(ExternalApiToken))


def _token(client) -> str:
    return client.post("/api/tokens", json={"name": "油猴"}).json()["token"]


def test_external_requires_token(client):
    resp = client.post("/api/add_task", json={"taskname": "t", "shareurl": "u", "savepath": "/s"})
    assert resp.status_code == 401
    tok = _token(client)
    resp = client.post(
        "/api/v1/task/add?token=" + tok, json={"taskname": "t", "shareurl": "u", "savepath": "/s"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] and body["code"] == 0 and body["data"]["taskname"] == "t"
    # Bearer 头方式
    resp = client.get("/api/v1/task/list", headers={"authorization": f"Bearer {tok}"})
    assert resp.json()["code"] == 0 and len(resp.json()["data"]) == 1


def test_add_task_missing_field_old_error_shape(client):
    tok = _token(client)
    body = client.post(f"/api/add_task?token={tok}", json={"taskname": "x"}).json()
    assert body == {"success": False, "code": 2, "message": "缺少必要字段: shareurl"}


def test_task_update_and_run_by_name(client, monkeypatch):
    tok = _token(client)
    client.post(
        f"/api/v1/task/add?token={tok}",
        json={
            "taskname": "B任务",
            "shareurl": "https://pan.quark.cn/s/b",
            "savepath": "/b",
            "update_subdir_resave_mode": True,
            "runweek": [2, 4],
        },
    )
    resp = client.post(
        f"/api/v1/task/update?token={tok}", json={"taskname": "B任务", "savepath": "/new", "disabled": True}
    )
    data = resp.json()["data"]
    assert data["savepath"] == "/new" and data["disabled"] and data["update_subdir_resave_mode"]
    assert data["runweek"] == [2, 4]

    called = {}

    async def fake_run(task_ids=None, trigger="manual"):
        called["ids"] = task_ids
        return {"run_id": "x", "total": 1}

    monkeypatch.setattr("backend.services.task_service.run_tasks", fake_run)
    resp = client.post(f"/api/v1/task/run?token={tok}", json={"taskname": "B任务"})
    assert resp.json()["code"] == 0 and called["ids"] == [data["id"]]


def test_tokens_list_and_delete(client):
    tok = _token(client)
    items = client.get("/api/tokens").json()
    assert items and tok not in json.dumps(items)  # 列表不泄漏完整 token
    assert client.request("DELETE", "/api/tokens", json={"token": tok}).json()["ok"]


def test_migrate_preview_and_import(client):
    prev = client.post("/api/migrate/preview", json={"config": OLD_CONFIG}).json()
    assert prev["accounts"] == 2 and prev["tasks"] == 2
    assert prev["plugin_tasks_ignored"] == ["旧任务A"]

    assert client.post("/api/migrate/preview", json={"config": {"foo": 1}}).status_code == 400

    res = client.post("/api/migrate", json={"config": OLD_CONFIG}).json()
    assert res["ok"] and res["imported_accounts"] == 2 and res["imported_tasks"] == 1  # 缺字段任务跳过

    # 再次导入未加 overwrite → 409
    assert client.post("/api/migrate", json={"config": OLD_CONFIG}).status_code == 409
    # 覆盖导入
    res = client.post("/api/migrate", json={"config": OLD_CONFIG, "overwrite": True}).json()
    assert res["ok"]

    tasks = client.get("/api/tasks").json()
    t = tasks[0]
    assert t["taskname"] == "旧任务A" and t["ignore_extension"] and t["update_subdir_resave"]
    assert t["runweek"] == [1, 3] and t["enddate"] == "2026-12-31" and t["pattern"] == "$TV"

    settings = client.get("/api/settings").json()
    assert settings["crontab"] == "15 10 * * *"
    assert settings["push_config"]["PUSH_KEY"] == "SCT123"
    assert settings["magic_regex"]["$DEMO"]["pattern"] == "x"
