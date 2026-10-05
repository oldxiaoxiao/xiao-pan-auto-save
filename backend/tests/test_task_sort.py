"""任务列表排序：新建置顶（sort_order = 现最小值 − 1）+ 显式置顶/置底端点。

排序断言依赖全表 min/max，故本模块前后清空 task 表（临时库，见 conftest 说明）；
用例内也只认自己创建的 id，避免与其他模块共享会话库的残留数据互相干扰。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete

from backend.database import session_scope
from backend.main import app
from backend.models import Task


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_tasks():
    with session_scope() as s:
        s.exec(delete(Task))
    yield
    with session_scope() as s:
        s.exec(delete(Task))


def _create(client: TestClient, name: str, **extra) -> dict:
    body = {"taskname": name, "shareurl": f"https://pan.quark.cn/s/{name}", "savepath": f"/{name}", **extra}
    resp = client.post("/api/tasks", json=body)
    assert resp.status_code == 200
    return resp.json()


def _get(client: TestClient, task_id: int) -> dict:
    return next(t for t in client.get("/api/tasks").json() if t["id"] == task_id)


def _order(client: TestClient) -> list[int]:
    return [t["id"] for t in client.get("/api/tasks").json()]


def test_first_task_on_empty_table_is_zero(client):
    """空表时第一个任务 sort_order 为 0（不是 -1）。"""
    created = _create(client, "空表首任务")
    assert created["sort_order"] == 0
    assert _order(client) == [created["id"]]


def test_three_created_in_sequence_newest_first(client):
    t1 = _create(client, "首发")
    t2 = _create(client, "次发")
    t3 = _create(client, "末发")

    assert (t1["sort_order"], t2["sort_order"], t3["sort_order"]) == (0, -1, -2)
    # GET /api/tasks 按 sort_order, id 升序：最新建在最前
    assert _order(client) == [t3["id"], t2["id"], t1["id"]]


def test_new_task_still_first_after_drag_renumber(client):
    t1 = _create(client, "拖前1")
    t2 = _create(client, "拖前2")
    t3 = _create(client, "拖前3")

    # 模拟拖拽：把顺序改成 t3, t1, t2 并重编号 0..n-1
    for i, task in enumerate([t3, t1, t2]):
        assert client.put(f"/api/tasks/{task['id']}", json={**task, "sort_order": i}).status_code == 200
    assert _order(client) == [t3["id"], t1["id"], t2["id"]]

    fresh = _create(client, "拖后新建")
    assert fresh["sort_order"] == -1  # 不与当前最小值 0 相撞
    assert _order(client) == [fresh["id"], t3["id"], t1["id"], t2["id"]]


def test_position_top_moves_middle_first_and_bottom_last(client):
    t1 = _create(client, "置顶1")
    t2 = _create(client, "置顶2")
    t3 = _create(client, "置顶3")
    assert _order(client) == [t3["id"], t2["id"], t1["id"]]

    top = client.post(f"/api/tasks/{t2['id']}/position", params={"where": "top"})
    assert top.status_code == 200
    assert top.json() == {"ok": True, "sort_order": -3}  # 现最小值 -2 再前一格
    assert _order(client)[:3] == [t2["id"], t3["id"], t1["id"]]

    bottom = client.post(f"/api/tasks/{t2['id']}/position", params={"where": "bottom"})
    assert bottom.status_code == 200
    assert bottom.json()["sort_order"] == 1  # 现最大值 0 再后一格
    assert _order(client)[:3] == [t3["id"], t1["id"], t2["id"]]


def test_position_rejects_unknown_where(client):
    task = _create(client, "非法参数")
    resp = client.post(f"/api/tasks/{task['id']}/position", params={"where": "nonsense"})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "只能" in detail and "top" in detail and "bottom" in detail


def test_position_defaults_to_top(client):
    t1 = _create(client, "默认1")
    t2 = _create(client, "默认2")
    resp = client.post(f"/api/tasks/{t1['id']}/position")
    assert resp.status_code == 200
    assert resp.json()["sort_order"] == -2  # 现最小值 -1 再前一格
    assert _order(client) == [t1["id"], t2["id"]]


def test_position_unknown_task_404(client):
    resp = client.post("/api/tasks/424242/position", params={"where": "top"})
    assert resp.status_code == 404


def test_position_leaves_other_fields_untouched(client):
    task = _create(client, "字段不变", pattern=r"EP\d+", download_savepath="/flat", disabled=True)
    assert client.post(f"/api/tasks/{task['id']}/position", params={"where": "bottom"}).status_code == 200

    after = _get(client, task["id"])
    assert after["sort_order"] == 1
    for field in ("taskname", "shareurl", "savepath", "pattern", "download_savepath", "disabled", "schedule"):
        assert after[field] == task[field], field
