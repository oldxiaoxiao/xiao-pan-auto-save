"""API 冒烟：任务/账号 CRUD、设置、空运行编排。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health_and_drivers(client):
    assert client.get("/api/health").json()["status"] == "ok"
    drivers = client.get("/api/drivers").json()
    assert any(d["key"] == "quark" and d["supported"] for d in drivers)


def test_task_crud(client):
    body = {
        "taskname": "测试任务",
        "shareurl": "https://pan.quark.cn/s/abc",
        "savepath": "/动漫/测试",
        "pattern": "",
        "replace": "",
        "runweek": [1, 3, 5],
        "enddate": "",
        "ignore_extension": True,
        "startfid": "",
        "update_subdir": "",
        "update_subdir_resave": False,
        "disabled": False,
        "account_id": None,
        "sort_order": 0,
    }
    created = client.post("/api/tasks", json=body).json()
    assert created["id"] and created["runweek"] == [1, 3, 5]

    tasks = client.get("/api/tasks").json()
    assert any(t["id"] == created["id"] for t in tasks)

    body["taskname"] = "改名任务"
    updated = client.put(f"/api/tasks/{created['id']}", json=body).json()
    assert updated["taskname"] == "改名任务"

    assert client.delete(f"/api/tasks/{created['id']}").json()["ok"]
    assert client.put(f"/api/tasks/{created['id']}", json=body).status_code == 404


def test_account_crud_and_masking(client):
    body = {
        "name": "小号",
        "driver_key": "quark",
        "cookie": "SECRET=verylongvalue123; __uid=t",
        "enabled": True,
        "sort_order": 0,
    }
    created = client.post("/api/accounts", json=body).json()
    assert "cookie" not in created
    assert "verylong" not in created["cookie_masked"]

    assert client.post("/api/accounts", json={**body, "driver_key": "nope"}).status_code == 400
    assert client.post("/api/accounts", json={**body, "cookie": ""}).status_code == 400

    # 部分更新：cookie 留空表示保持原值，不得被掩码覆盖
    updated = client.put(f"/api/accounts/{created['id']}", json={**body, "cookie": "", "name": "改名"}).json()
    assert updated["name"] == "改名" and updated["cookie_masked"].startswith("SECRET")

    assert client.delete(f"/api/accounts/{created['id']}").json()["ok"]


def test_settings_roundtrip(client):
    settings = client.get("/api/settings").json()
    assert "crontab" in settings and "push_config" in settings
    resp = client.put("/api/settings/crontab", json={"value": "30 8 * * *"})
    assert resp.json()["value"] == "30 8 * * *"
    assert client.put("/api/settings/secret_key", json={"value": 1}).status_code == 404
    # 还原默认
    client.put("/api/settings/crontab", json={"value": "0 9 * * *"})


def test_scheduler_info(client):
    info = client.get("/api/scheduler").json()
    assert info["next_run"]  # 主任务已排期


def test_run_all_with_no_tasks(client):
    # 无任务时 SSE 应直接走完并返回 summary
    with client.stream("POST", "/api/tasks/run") as resp:
        assert resp.status_code == 200
        events = [line for line in resp.iter_lines() if line.startswith("data:")]
    assert any('"level": "done"' in e or '"level":"done"' in e for e in events)
    assert any("summary" in e for e in events)


def test_task_episode_quality_fields(client):
    body = {
        "taskname": "集数画质任务",
        "shareurl": "https://pan.quark.cn/s/xyz",
        "savepath": "/动漫/剧",
        "episode_start": 1,
        "episode_end": 20,
        "quality": "1080p,4k",
    }
    created = client.post("/api/tasks", json=body).json()
    assert created["episode_start"] == 1 and created["episode_end"] == 20
    assert created["quality"] == "1080p,4k"

    # 默认值向后兼容：不传即 0/0/""
    plain = client.post(
        "/api/tasks",
        json={"taskname": "默认", "shareurl": "https://pan.quark.cn/s/d", "savepath": "/d"},
    ).json()
    assert plain["episode_start"] == 0 and plain["episode_end"] == 0 and plain["quality"] == ""
    client.delete(f"/api/tasks/{created['id']}")
    client.delete(f"/api/tasks/{plain['id']}")


def test_task_schedule_wiring(client, monkeypatch):
    """任务 CRUD → 调度器接线：monkeypatch 记录调用，不依赖真实 APScheduler 时序。"""
    from backend.main import scheduler

    calls = []
    monkeypatch.setattr(
        scheduler,
        "reschedule_task",
        lambda task_id, schedule, func: calls.append(("reschedule", task_id, schedule)) or "fake",
    )
    monkeypatch.setattr(scheduler, "unschedule_task", lambda task_id: calls.append(("unschedule", task_id)))

    body = {
        "taskname": "调度接线",
        "shareurl": "https://pan.quark.cn/s/wiring",
        "savepath": "/wiring",
        "schedule": "interval:5",
    }
    created = client.post("/api/tasks", json=body).json()
    tid = created["id"]
    # 创建（interval:5）→ reschedule_task(tid, "interval:5", func)
    assert ("reschedule", tid, "interval:5") in calls

    # 更新为空 schedule → apply_task_schedule 走 reschedule_task(tid, "")（其内部撤销旧 job）
    body["schedule"] = ""
    client.put(f"/api/tasks/{tid}", json=body)
    assert ("reschedule", tid, "") in calls

    # 更新为非法 cron → 200 不崩（真实实现经 FIX 1：撤销旧 job + warning 告警）
    body["schedule"] = "cron:bad-cron"
    assert client.put(f"/api/tasks/{tid}", json=body).status_code == 200
    assert ("reschedule", tid, "cron:bad-cron") in calls

    # 删除 → unschedule_task(tid)
    client.delete(f"/api/tasks/{tid}")
    assert ("unschedule", tid) in calls


async def test_global_sweep_skips_tasks_with_own_schedule(client, monkeypatch):
    """FIX 3：全局 sweep 只驱动无有效独立调度的任务；有效 schedule 的任务被跳过，空/非法仍参与。"""
    from backend.models import Task
    from backend.services import task_service

    tasks = [
        Task(id=901, taskname="自带调度", shareurl="https://fake.example/s", savepath="/s", schedule="cron:0 9 * * 0"),
        Task(id=902, taskname="非法调度", shareurl="https://fake.example/s", savepath="/s", schedule="cron:bad"),
        Task(id=903, taskname="继承全局", shareurl="https://fake.example/s", savepath="/s", schedule=""),
    ]
    monkeypatch.setattr(
        task_service, "load_tasks", lambda ids=None: [t for t in tasks if ids is None or t.id in ids]
    )
    routed = []
    monkeypatch.setattr(task_service, "route_driver", lambda url: routed.append(url) or None)

    async def no_push(*a, **kw):
        return None

    monkeypatch.setattr(task_service, "_push", no_push)

    summary = await task_service.run_tasks(trigger="scheduled")  # task_ids=None 即全局 sweep
    assert summary["skipped"] == 1  # 901 有有效独立调度，sweep 不双驱动
    assert summary["failed"] == 2  # 902（非法）/903（空）仍由 sweep 兜底
    assert len(routed) == 2

    # 指定任务运行（非 sweep）不受影响：有效 schedule 的任务照常执行
    routed.clear()
    await task_service.run_tasks(task_ids=[901], trigger="scheduled")
    assert routed
