"""新建任务相关默认值：TaskIn 默认、task_defaults 设置键、$TV 展开端点。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.main import app
from backend.schemas import TaskIn


def _body(**extra):
    return {"taskname": "剧集", "shareurl": "https://pan.quark.cn/s/abc", "savepath": "/剧", **extra}


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
        assert c.get("/api/settings").json()["task_defaults"]["savepath_root"] == "/来自：分享"
        put = c.put("/api/settings/task_defaults", json={"value": {"savepath_root": "/追剧"}})
        assert put.status_code == 200
        merged = c.get("/api/settings").json()["task_defaults"]
        # 部分写入不能被当成整份替换后丢字段：缺的键仍要有默认值
        assert merged["savepath_root"] == "/追剧" and merged["subdir_filter"] is True
        assert merged["auto_download"] is True and merged["run_mode"] == "follow"
        # 别的键不受影响
        assert c.get("/api/settings").json()["crontab"]
        c.put("/api/settings/task_defaults", json={"value": dict(DEFAULT_RESTORE)})


DEFAULT_RESTORE = {
    "savepath_root": "/来自：分享",
    "auto_download": True,
    "run_mode": "follow",
    "pattern": "",
    "quality": "",
    "subdir_filter": True,
}


def test_magic_expand_returns_real_regex_without_copying_constants():
    """前端不许手抄 $TV 正则；展开只住在 MagicRename 一处。"""
    with TestClient(app) as c:
        got = c.get("/api/settings/magic/expand", params={"name": "$TV"}).json()
        assert got["ok"] is True and "mp4" in got["pattern"] and got["replace"]
        assert c.get("/api/settings/magic/expand", params={"name": "$NOPE"}).json()["ok"] is False
