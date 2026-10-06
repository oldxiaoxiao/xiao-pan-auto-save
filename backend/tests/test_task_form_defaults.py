"""新建任务相关默认值：TaskIn 默认、task_defaults 设置键、$TV 展开端点。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.api.deps import DEFAULT_SETTINGS
from backend.database import session_scope
from backend.main import app
from backend.models import Task
from backend.schemas import TaskIn


def _body(**extra):
    return {"taskname": "剧集", "shareurl": "https://pan.quark.cn/s/abc", "savepath": "/剧", **extra}


def _auto_download_in_db(task_id: int) -> bool:
    """落库口径直接查模型，不经过任何响应模型的默认值兜底。"""
    with session_scope() as s:
        return s.get(Task, task_id).auto_download


def _delete_task(task_id: int) -> None:
    with session_scope() as s:
        s.delete(s.get(Task, task_id))


def test_taskin_auto_download_default_is_true_now():
    """破坏面回归：省略该字段的外部调用方会开始下载，这条测试就是把它钉在明处。"""
    assert TaskIn(**_body()).auto_download is True
    assert TaskIn(**_body(auto_download=False)).auto_download is False  # 显式关仍然有效


def test_taskin_update_subdir_default_stays_empty():
    """目录内过滤的默认只归前端管，后端协议层保持沉默。"""
    assert TaskIn(**_body()).update_subdir == ""


def test_task_defaults_ship_with_known_shape():
    from backend.api.deps import DEFAULT_SETTINGS

    defaults = DEFAULT_SETTINGS["task_defaults"]
    assert defaults == {
        "savepath_root": "/来自：分享",
        "auto_download": True,
        "run_mode": "follow",
        "pattern": "",
        "quality": "",
        "subdir_filter": True,
    }


def test_task_defaults_roundtrip_and_not_shadowing_others():
    with TestClient(app) as c:
        try:
            assert c.get("/api/settings").json()["task_defaults"]["savepath_root"] == "/来自：分享"
            put = c.put("/api/settings/task_defaults", json={"value": {"savepath_root": "/追剧"}})
            assert put.status_code == 200
            merged = c.get("/api/settings").json()["task_defaults"]
            # 部分写入不能被当成整份替换后丢字段：缺的键仍要有默认值
            assert merged["savepath_root"] == "/追剧" and merged["subdir_filter"] is True
            assert merged["auto_download"] is True and merged["run_mode"] == "follow"
            # 别的键不受影响
            assert c.get("/api/settings").json()["crontab"]
        finally:
            # 恢复必须在 finally：断言中途失败也不能把 "/追剧" 留在共享临时库里；
            # 出厂形状直接从 DEFAULT_SETTINGS 取，不再手抄第三遍
            restore = dict(DEFAULT_SETTINGS["task_defaults"])
            c.put("/api/settings/task_defaults", json={"value": restore})


def test_get_settings_merges_bypass_partial_task_defaults():
    """绕过写接口直接 set_setting 存半份时，GET /api/settings 读侧要补齐六键——这条钉住 read_settings 里那次 merge。"""
    from backend.api.deps import set_setting

    with TestClient(app) as c:
        try:
            set_setting("task_defaults", {"savepath_root": "/半份"})  # 模拟旁路写入的半份值
            merged = c.get("/api/settings").json()["task_defaults"]
            assert merged["savepath_root"] == "/半份"
            # 其余五个键必须等于出厂默认，且六键齐全
            for key, value in DEFAULT_SETTINGS["task_defaults"].items():
                if key != "savepath_root":
                    assert merged[key] == value
            assert set(merged) == set(DEFAULT_SETTINGS["task_defaults"])
        finally:
            set_setting("task_defaults", dict(DEFAULT_SETTINGS["task_defaults"]))


# —— /api/add_task 外部路径的 auto_download 接线（spec §4.2/§5.1 破坏面必须是真的） ——


def test_external_add_task_defaults_auto_download_on():
    """油猴走 /api/add_task 省略 auto_download：落库必须是 True（默认下载到本地，与网页新建一致）。"""
    with TestClient(app) as c:
        tok = c.post("/api/tokens", json={"name": "油猴"}).json()["token"]
        resp = c.post(f"/api/add_task?token={tok}", json=_body(taskname="外部默认开"))
        assert resp.status_code == 200 and resp.json()["success"]
        task_id = resp.json()["data"]["id"]
        try:
            assert _auto_download_in_db(task_id) is True
        finally:
            with session_scope() as s:
                s.delete(s.get(Task, task_id))


def test_external_add_task_explicit_false_kept():
    """显式传 false 才关：外部调用方要不下载，落库必须是 False。"""
    with TestClient(app) as c:
        tok = c.post("/api/tokens", json={"name": "油猴"}).json()["token"]
        resp = c.post(
            f"/api/v1/task/add?token={tok}", json=_body(taskname="外部显式关", auto_download=False)
        )
        assert resp.status_code == 200 and resp.json()["success"]
        task_id = resp.json()["data"]["id"]
        try:
            assert _auto_download_in_db(task_id) is False
        finally:
            with session_scope() as s:
                s.delete(s.get(Task, task_id))


def test_external_update_can_turn_auto_download_off():
    """外部更新路径（/api/v1/task/update）要能把 auto_download 从 True 改成 False。"""
    with TestClient(app) as c:
        tok = c.post("/api/tokens", json={"name": "油猴"}).json()["token"]
        resp = c.post(f"/api/add_task?token={tok}", json=_body(taskname="外部改开关"))
        task_id = resp.json()["data"]["id"]
        try:
            assert _auto_download_in_db(task_id) is True  # 新建默认开
            upd = c.post(
                f"/api/v1/task/update?token={tok}",
                json={"id": task_id, "auto_download": False},
            )
            assert upd.status_code == 200 and upd.json()["success"]
            assert _auto_download_in_db(task_id) is False
        finally:
            with session_scope() as s:
                s.delete(s.get(Task, task_id))


def test_magic_expand_returns_real_regex_without_copying_constants():
    """前端不许手抄 $TV 正则；展开只住在 MagicRename 一处。"""
    with TestClient(app) as c:
        got = c.get("/api/settings/magic/expand", params={"name": "$TV"}).json()
        assert got["ok"] is True and "mp4" in got["pattern"] and got["replace"]
        assert c.get("/api/settings/magic/expand", params={"name": "$NOPE"}).json()["ok"] is False
