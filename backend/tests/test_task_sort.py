"""任务列表排序：新建置顶（sort_order = 现最小值 − 1）+ 显式置顶/置底端点。

网页新建（POST /api/tasks）与对外新建（/api/add_task，油猴脚本）是两条路径，都得排到最前。
排序断言依赖全表 min/max，故本模块前后清空 task 表（临时库，见 conftest 说明）；
用例内也只认自己创建的 id，避免与其他模块共享会话库的残留数据互相干扰。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import delete

from backend.database import session_scope
from backend.main import app
from backend.models import ExternalApiToken, Task


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_tasks():
    # 对外接口那条用例会建 token，一并清掉免得泄漏给其他模块
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(ExternalApiToken))
    yield
    with session_scope() as s:
        s.exec(delete(Task))
        s.exec(delete(ExternalApiToken))


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


# —— 契约固化：以下三条把「实现顺手就会写歪」的行为钉住 ——


def test_create_task_ignores_client_supplied_sort_order(client):
    """建任务时请求里的 sort_order 不作数：表单恒发 0，照单收下就会和当前首行撞位。"""
    first = _create(client, "钉住1")
    second = _create(client, "钉住2", sort_order=5)

    assert second["sort_order"] == -1  # 现最小值 0 再前一格，而不是请求给的 5
    assert _order(client) == [second["id"], first["id"]]


def test_position_on_single_row_table(client):
    """只剩一行时 min == max，置顶/置底都得照常工作：各自相对当前值 ±1。"""
    only = _create(client, "孤行")
    assert client.put(f"/api/tasks/{only['id']}", json={**only, "sort_order": 5}).status_code == 200

    top = client.post(f"/api/tasks/{only['id']}/position", params={"where": "top"})
    assert top.status_code == 200
    assert top.json()["sort_order"] == 4  # 5 − 1

    bottom = client.post(f"/api/tasks/{only['id']}/position", params={"where": "bottom"})
    assert bottom.status_code == 200
    assert bottom.json()["sort_order"] == 5  # 4 + 1
    assert _order(client) == [only["id"]]


def test_position_rejects_bad_where_before_looking_up_id(client):
    """先校验参数再查行：id 不存在但 where 非法时仍是 400，不被 404 掩盖成「没这个任务」。"""
    resp = client.post("/api/tasks/424242/position", params={"where": "middle"})
    assert resp.status_code == 400
    assert "只能" in resp.json()["detail"]


# —— 对外接口（油猴脚本）建任务也必须排在最前 ——


def test_external_add_task_sorts_first(client):
    """/api/add_task 是另一条建任务路径（油猴脚本用），不显式给 sort_order 就会落回默认 0 垫底。"""
    _create(client, "网页建")  # 0
    _create(client, "网页建2")  # -1
    tok = client.post("/api/tokens", json={"name": "油猴"}).json()["token"]

    resp = client.post(
        f"/api/add_task?token={tok}",
        json={"taskname": "油猴加", "shareurl": "https://pan.quark.cn/s/ext", "savepath": "/ext"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] and body["code"] == 0

    ext_id = body["data"]["id"]
    assert _get(client, ext_id)["sort_order"] == -2  # 现最小值 −1 再前一格
    assert _order(client)[0] == ext_id
    # 对外列表接口同样按 sort_order 排：油猴侧看到的也是最新在前
    listing = client.get(f"/api/v1/task/list?token={tok}").json()["data"]
    assert [t["id"] for t in listing] == _order(client)
