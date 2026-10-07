"""文件浏览/分享预览/搜索 API 测试（驱动与网络全部 mock）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api import routes_files, routes_search
from backend.drivers.base import CloudDrive, DriveError, FsItem, ShareRef
from backend.main import app


class BrowseDriver(CloudDrive):
    key = "fake"
    name = "假盘"
    share_domains = ["fake.example"]
    supported = True
    capability = {"rename", "delete"}

    def __init__(self, **kw):
        super().__init__(**kw)
        self.renamed = []
        self.deleted = []

    def parse_share(self, url: str) -> ShareRef:
        return ShareRef(url=url, extra={"path_fids": {"": "0"}})

    async def list_share(self, ref, path=""):
        return [
            FsItem(fid="1", name="第05集 1080p.mp4", mtime=100),
            FsItem(fid="2", name="sample.mkv", mtime=90),
            FsItem(fid="3", name="4K", is_dir=True),
        ]

    async def list_dir(self, path):
        return [FsItem(fid="9", name="第05集.mp4")] if path == "/存" else []

    async def ensure_dir(self, path):
        return "f"

    async def save(self, items, dest_path, ref=None):
        raise NotImplementedError

    async def rename(self, item_or_fid, new_name):
        fid = item_or_fid.fid if isinstance(item_or_fid, FsItem) else item_or_fid
        self.renamed.append((fid, new_name))

    async def delete_items(self, items, purge=True):
        self.deleted.extend(i.fid for i in items)


@pytest.fixture
def client(monkeypatch):
    drv = BrowseDriver()
    monkeypatch.setattr(routes_files, "_driver", lambda key: drv)
    with TestClient(app) as c:
        c.drv = drv
        yield c


def test_list_dir(client):
    data = client.get("/api/files/dir", params={"path": "/存"}).json()
    assert data["at_root"] is False and data["parent"] == "/"
    assert data["list"][0]["name"] == "第05集.mp4"


def test_list_dir_cookie_error_friendly(client, monkeypatch):
    async def boom(path):
        raise DriveError("获取目录列表失败: code=31001 require login")

    monkeypatch.setattr(client.drv, "list_dir", boom)
    resp = client.get("/api/files/dir", params={"path": "/"})
    assert resp.status_code == 502
    assert "Cookie" in resp.json()["detail"]


def test_list_dir_generic_error_friendly(client, monkeypatch):
    async def boom(path):
        raise DriveError("获取目录列表失败: 磁盘配额不足")

    monkeypatch.setattr(client.drv, "list_dir", boom)
    resp = client.get("/api/files/dir", params={"path": "/"})
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "读取目录失败" in detail and "配额" in detail


def test_rename_and_delete(client):
    ok = client.post("/api/files/rename", json={"path": "/存/第05集.mp4", "new_name": "E05.mp4"}).json()
    assert ok["ok"]
    assert client.drv.renamed == [("9", "E05.mp4")]
    ok = client.post("/api/files/delete", json={"path": "/存/第05集.mp4"}).json()
    assert ok["ok"] and client.drv.deleted == ["9"]
    assert (
        client.post("/api/files/rename", json={"path": "/不存在/x.mp4", "new_name": "y"}).status_code == 404
    )


def test_share_preview_with_regex_column(client):
    body = {
        "shareurl": "https://fake.example/s/abc",
        "taskname": "某剧",
        "pattern": r"第(\d+)集.*?\.mp4",
        "replace": r"第\1集.mp4",
        "savepath": "/存",
    }
    import backend.core.router as router

    orig = router.route_driver
    router.route_driver = lambda url: BrowseDriver  # type: ignore
    try:
        data = client.post("/api/files/share/preview", json=body).json()
    finally:
        router.route_driver = orig  # type: ignore
    assert data["ok"]
    rows = {r["name"]: r for r in data["list"]}
    assert rows["第05集 1080p.mp4"]["name_re"] == "第05集.mp4"
    assert rows["第05集 1080p.mp4"]["saved_as"] == "第05集.mp4"  # 目标目录已存在
    assert "name_re" not in rows["4K"]  # 目录未匹配 pattern（设置 update_subdir 后才参与）
    assert "name_re" not in rows["sample.mkv"]  # 未匹配 pattern


def test_search_suggestions_endpoint(client, monkeypatch):
    async def fake_search_all(q, deep, cfg, **kw):
        return {
            "data": [
                {
                    "shareurl": "https://pan.quark.cn/s/x",
                    "taskname": q,
                    "datetime": "",
                    "content": "",
                    "channel": "",
                    "source": "PanSou",
                }
            ],
            "token_updates": {},
            "errors": [],
        }

    monkeypatch.setattr(routes_search, "search_all", fake_search_all)
    data = client.get("/api/search/suggestions", params={"q": "三体"}).json()
    assert data["ok"] and data["data"][0]["taskname"] == "三体"
