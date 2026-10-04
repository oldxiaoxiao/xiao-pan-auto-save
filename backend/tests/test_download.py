"""下载服务测试：路径安全、目录递归、内置下载、aria2 协议、Emby 触发。"""

from __future__ import annotations

import pytest

from backend.core.engine import SavedFile
from backend.drivers.base import CloudDrive, FsItem, ShareRef
from backend.services import download_service as dl
from backend.services.download_service import DownloadSettings, resolve_local, safe_name


class DlDriver(CloudDrive):
    key = "fake"
    name = "假盘"
    supported = True
    capability = {"download"}
    UA = "FakeUA/9.9"

    def __init__(self, children: dict[str, list[FsItem]] | None = None):
        super().__init__()
        self.children = children or {}
        self.requested: list[list[str]] = []

    def parse_share(self, url):
        return ShareRef(url=url)

    async def list_share(self, ref, path=""):
        return []

    async def list_dir(self, path):
        return []

    async def ensure_dir(self, path):
        return "f"

    async def save(self, items, dest_path, ref=None):
        raise NotImplementedError

    async def list_dir_children(self, fid):
        return self.children.get(fid, [])

    async def get_download_urls(self, fids):
        self.requested.append(list(fids))
        return [
            {"fid": f, "file_name": f, "size": 10, "download_url": f"http://dl/{f}"} for f in fids
        ], "DLCK=1"


def cfg(tmp_path, **kw) -> DownloadSettings:
    base = dict(mode="builtin", dir=str(tmp_path / "down"), concurrency=2)
    base.update(kw)
    return DownloadSettings(**base)


def saved(fid="1", name="01.mp4", dest="/动漫/剧/01.mp4", is_dir=False) -> SavedFile:
    return SavedFile(share_name=name, final_name=name, new_fid=fid, dest_path=dest, is_dir=is_dir)


def test_safe_name_blocks_traversal():
    assert safe_name("../../etc/passwd") == "_.._etc_passwd"
    assert safe_name("a/b\\c:d*e") == "a_b_c_d_e"
    assert safe_name("") == "_"
    p = resolve_local("/动漫/剧/01.mp4", DownloadSettings(dir="/base"), "")
    assert str(p).replace("\\", "/").endswith("/base/动漫/剧/01.mp4")
    evil = resolve_local("/x/../../y/1.mp4", DownloadSettings(dir="/base"), "")
    assert "/.." not in str(evil)
    assert evil.name == "1.mp4"


def test_resolve_local_override_flattens(tmp_path):
    c = cfg(tmp_path)
    p = resolve_local("/动漫/剧/01.mp4", c, "剧集专区")
    assert p == tmp_path / "down" / "剧集专区" / "01.mp4"


@pytest.mark.asyncio
async def test_collect_files_recursive(tmp_path):
    driver = DlDriver(
        children={
            "d1": [FsItem(fid="s1", name="a.mp4", size=5), FsItem(fid="d2", name="sub", is_dir=True)],
            "d2": [FsItem(fid="s2", name="b.mp4", size=7)],
        }
    )
    items = await dl.collect_files(
        driver, [saved(fid="d1", name="4K", dest="/动漫/4K", is_dir=True)], cfg(tmp_path), "", True
    )
    paths = sorted(str(i.local_path).replace("\\", "/") for i in items)
    assert any(p.endswith("4K/a.mp4") for p in paths)
    assert any(p.endswith("4K/sub/b.mp4") for p in paths)
    # 不递归时目录不入列
    assert (
        await dl.collect_files(
            driver, [saved(fid="d1", dest="/动漫/4K", is_dir=True)], cfg(tmp_path), "", False
        )
        == []
    )


@pytest.mark.asyncio
async def test_builtin_download_flow(tmp_path, monkeypatch):
    driver = DlDriver()
    calls = []

    async def fake_fetch(row, item, cookie_str, ua, *, job_id=None):
        calls.append((row["download_url"], str(item.local_path), cookie_str, ua))
        return True, f"{item.name}（0.0MB）"

    monkeypatch.setattr(dl, "_fetch_one", fake_fetch)
    emby = []
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: emby.append(1) or _noop())

    lines = await dl.download_task_files(
        driver, [saved("1"), saved("2", "02.mp4", "/动漫/剧/02.mp4")], cfg(tmp_path)
    )
    assert len(lines) == 2 and all(s.startswith("✅") for s in lines)
    assert calls[0][0] == "http://dl/1" and calls[0][2] == "DLCK=1" and calls[0][3] == "FakeUA/9.9"
    assert emby == [1]  # 成功后触发刷新


async def _noop():
    return None


@pytest.mark.asyncio
async def test_fetch_one_writes_and_skips(tmp_path, monkeypatch):
    class FakeResp:
        status_code = 200

        async def aiter_bytes(self, _n):
            yield b"x" * 10

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            assert headers["cookie"] == "C=1" and headers["user-agent"] == "UA"
            return FakeResp()

    monkeypatch.setattr(dl.httpx, "AsyncClient", FakeClient)
    target = tmp_path / "sub" / "01.mp4"
    item = dl.DownloadItem(fid="1", name="01.mp4", size=10, local_path=target)
    ok, msg = await dl._fetch_one({"download_url": "http://dl/1", "size": 10}, item, "C=1", "UA")
    assert ok and target.read_bytes() == b"x" * 10 and not list(target.parent.glob("*.part"))
    # 已存在同大小 → 跳过
    ok2, msg2 = await dl._fetch_one({"download_url": "http://dl/1", "size": 10}, item, "C=1", "UA")
    assert ok2 and "跳过" in msg2


@pytest.mark.asyncio
async def test_aria2_payload_protocol(tmp_path, monkeypatch):
    posted = []

    class FakeResp:
        def json(self):
            return {"result": "gid-1"}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posted.append((url, json))
            return FakeResp()

    monkeypatch.setattr(dl.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(dl, "aria2_reachable", lambda c: _ret(True))
    driver = DlDriver()
    c = cfg(
        tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800", aria2_secret="sec", aria2_pause=True
    )
    lines = await dl.download_task_files(driver, [saved("1")], c)
    assert lines[0].startswith("✅")
    url, payload = posted[0]
    assert url == "http://127.0.0.1:6800/jsonrpc"
    assert payload["method"] == "aria2.addUri"
    assert payload["params"][0] == "token:sec"
    opts = payload["params"][2]
    assert opts["pause"] == "true"
    assert opts["out"] == "01.mp4"
    assert "Cookie: DLCK=1" in opts["header"]
    assert "User-Agent: FakeUA/9.9" in opts["header"]
    assert payload["params"][1] == ["http://dl/1"]


@pytest.mark.asyncio
async def test_no_saved_files_noop(tmp_path):
    assert await dl.download_task_files(DlDriver(), [], cfg(tmp_path)) == []


@pytest.mark.asyncio
async def test_aria2_unreachable_falls_back_to_builtin(tmp_path, monkeypatch):
    """aria2 模式但连不上时，自动降级到内置下载器，保证下载落盘。"""
    driver = DlDriver()
    monkeypatch.setattr(dl, "aria2_reachable", lambda c: _ret(False))
    fetched = []

    async def fake_fetch(row, item, cookie_str, ua, *, job_id=None):
        fetched.append(item.name)
        return True, f"{item.name} ok"

    monkeypatch.setattr(dl, "_fetch_one", fake_fetch)
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())
    c = cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:1")
    lines = await dl.download_task_files(driver, [saved("1")], c)
    assert fetched == ["01.mp4"]  # 走了内置 _fetch_one
    assert lines[0].startswith("✅")


@pytest.mark.asyncio
async def test_aria2_reachable_uses_aria2(tmp_path, monkeypatch):
    """aria2 可达时仍走 aria2 投递，不降级。"""
    driver = DlDriver()
    monkeypatch.setattr(dl, "aria2_reachable", lambda c: _ret(True))
    submitted = []

    async def fake_submit(driver_, items, cfg_, log):
        submitted.append(len(items))
        return ["✅ aria2 已投递 01.mp4"]

    monkeypatch.setattr(dl, "_aria2_submit", fake_submit)
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())
    c = cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800")
    lines = await dl.download_task_files(driver, [saved("1")], c)
    assert submitted == [1] and lines[0].startswith("✅")


async def _ret(v):
    return v


@pytest.mark.asyncio
async def test_aria2_status_maps_active_and_waiting(tmp_path, monkeypatch):
    class FakeResp:
        def __init__(self, result):
            self._result = result

        def json(self):
            return {"result": self._result}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            if json["method"] == "aria2.tellActive":
                return FakeResp(
                    [
                        {
                            "gid": "g1",
                            "status": "active",
                            "totalLength": "1000",
                            "completedLength": "250",
                            "downloadSpeed": "50",
                            "files": [{"path": "/d/凡人/193.mkv"}],
                        },
                        {
                            "gid": "g3",
                            "status": "paused",
                            "totalLength": "500",
                            "completedLength": "100",
                            "downloadSpeed": "0",
                            "files": [{"path": "/d/凡人/194.mkv"}],
                        },
                    ]
                )
            return FakeResp(
                [
                    {
                        "gid": "g2",
                        "status": "waiting",
                        "totalLength": "0",
                        "completedLength": "0",
                        "downloadSpeed": "0",
                        "files": [],
                    }
                ]
            )

    monkeypatch.setattr(dl.httpx, "AsyncClient", FakeClient)
    c = cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800", aria2_secret="sec")
    jobs = await dl.aria2_status(c)
    assert len(jobs) == 3
    assert all(j["source"] == "aria2" for j in jobs)
    by_id = {j["id"]: j for j in jobs}
    a = by_id["g1"]
    assert a["status"] == "downloading" and a["total"] == 1000 and a["done"] == 250
    assert a["speed"] == 50.0 and a["filename"] == "193.mkv" and a["taskname"] == ""
    assert by_id["g2"]["status"] == "queued" and by_id["g2"]["filename"] == "aria2"
    assert by_id["g3"]["status"] == "paused"  # aria2 paused → paused，不再误报 queued


@pytest.mark.asyncio
async def test_aria2_status_disabled_returns_empty(tmp_path):
    assert await dl.aria2_status(cfg(tmp_path)) == []  # builtin 模式
    c = cfg(tmp_path, mode="aria2")  # 未配 RPC
    assert await dl.aria2_status(c) == []


@pytest.mark.asyncio
async def test_aria2_status_unreachable_degrades(tmp_path, monkeypatch):
    class Boom:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            raise OSError("conn refused")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(dl.httpx, "AsyncClient", Boom)
    c = cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800")
    assert await dl.aria2_status(c) == []


@pytest.mark.asyncio
async def test_fetch_one_cancellation_removes_part(tmp_path, monkeypatch):
    from backend.core.download_registry import registry

    class Resp:
        status_code = 200

        async def aiter_bytes(self, _n):
            yield b"x" * 10
            yield b"y" * 10

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        def __init__(self, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, m, u, headers=None):
            return Resp()

    monkeypatch.setattr(dl.httpx, "AsyncClient", Client)
    target = tmp_path / "c.mkv"
    item = dl.DownloadItem(fid="c", name="c.mkv", size=999, local_path=target)
    jid = registry.create(task_id=None, taskname="T", filename="c.mkv", dest_path=str(target), total=999)
    registry.stop(jid)  # 立即请求取消
    ok, msg = await dl._fetch_one({"download_url": "http://x", "size": 999}, item, "", "UA", job_id=jid)
    assert ok is False and "已停止" in msg
    assert not target.exists() and not list(tmp_path.glob("*.part"))
    assert next(j for j in registry.snapshot() if j["id"] == jid)["status"] == "stopped"


@pytest.mark.asyncio
async def test_fetch_one_reports_progress_and_done(tmp_path):
    from backend.core.download_registry import registry

    class FakeResp:
        status_code = 200
        async def aiter_bytes(self, _n):
            yield b"x" * 10
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, method, url, headers=None): return FakeResp()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dl.httpx, "AsyncClient", FakeClient)
    target = tmp_path / "sub" / "01.mp4"
    item = dl.DownloadItem(fid="1", name="01.mp4", size=10, local_path=target)
    jid = registry.create(task_id=None, taskname="T", filename="01.mp4", dest_path=str(target), total=10)
    ok, msg = await dl._fetch_one({"download_url": "http://dl/1", "size": 10}, item, "C=1", "UA", job_id=jid)
    monkeypatch.undo()
    job = next(j for j in registry.snapshot() if j["id"] == jid)
    assert ok and job["status"] == "done" and job["done"] == 10


@pytest.mark.asyncio
async def test_fetch_one_reports_skipped_and_failed(tmp_path):
    from backend.core.download_registry import registry

    # 已存在同大小 → skipped
    target = tmp_path / "s.mp4"
    target.write_bytes(b"y" * 10)
    item = dl.DownloadItem(fid="s", name="s.mp4", size=10, local_path=target)
    jid = registry.create(task_id=None, taskname="T", filename="s.mp4", dest_path=str(target), total=10)
    ok, msg = await dl._fetch_one({"download_url": "http://dl", "size": 10}, item, "", "UA", job_id=jid)
    assert ok and "跳过" in msg
    assert next(j for j in registry.snapshot() if j["id"] == jid)["status"] == "skipped"

    # HTTP 非 200 → failed
    class BadResp:
        status_code = 403
        async def aiter_bytes(self, _n): yield b""
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    class BadClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        def stream(self, method, url, headers=None): return BadResp()
    mp = pytest.MonkeyPatch()
    mp.setattr(dl.httpx, "AsyncClient", BadClient)
    item2 = dl.DownloadItem(fid="b", name="b.mp4", size=5, local_path=tmp_path / "b.mp4")
    jid2 = registry.create(task_id=None, taskname="T", filename="b.mp4", dest_path=str(item2.local_path), total=5)
    ok2, _ = await dl._fetch_one({"download_url": "http://dl", "size": 5}, item2, "", "UA", job_id=jid2)
    mp.undo()
    job2 = next(j for j in registry.snapshot() if j["id"] == jid2)
    assert not ok2 and job2["status"] == "failed" and "403" in job2["error"]


@pytest.mark.asyncio
async def test_aria2_submit_sends_absolute_dir(tmp_path, monkeypatch):
    """回归：aria2 投递的 dir 必须是绝对路径（否则容器按自身 CWD 解析，文件落不进宿主机挂载）。"""
    import os

    posted = []

    class R:
        def json(self):
            return {"result": "g1"}

    class C:
        def __init__(self, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posted.append(json)
            return R()

    monkeypatch.setattr(dl.httpx, "AsyncClient", C)
    monkeypatch.setattr(dl, "aria2_reachable", lambda c: _ret(True))
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())
    monkeypatch.chdir(tmp_path)  # 相对 dir 在此临时目录下解析
    c = DownloadSettings(
        mode="aria2",
        dir="data/downloads-rel",
        concurrency=2,
        aria2_host_port="http://127.0.0.1:6800",
        aria2_secret="",
        aria2_pause=False,
    )
    await dl.download_task_files(DlDriver(), [saved("1")], c)
    opts = posted[0]["params"][-1]
    assert os.path.isabs(opts["dir"]), opts["dir"]
    assert opts["dir"].replace("\\", "/").endswith("data/downloads-rel/动漫/剧")
