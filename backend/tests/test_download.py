"""下载服务测试：路径安全、目录递归、内置下载、aria2 协议、Emby 触发。"""

from __future__ import annotations

import os
import re

import pytest
from sqlmodel import col, select

from backend.core.engine import SavedFile
from backend.database import session_scope
from backend.drivers.base import CloudDrive, FsItem, ShareRef
from backend.models import DownloadRecord
from backend.services import download_history as hist
from backend.services import download_service as dl
from backend.services.download_service import DownloadSettings, resolve_local, safe_name


class DlDriver(CloudDrive):
    key = "fake"
    name = "假盘"
    supported = True
    capability = {"download"}
    UA = "FakeUA/9.9"

    def __init__(self, children: dict[str, list[FsItem]] | None = None, **kw):
        # **kw 透传 CloudDrive 的 (cookie/proxy/index)：生产代码按该签名构造驱动（重下路径要用）
        super().__init__(**kw)
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

    async def fake_submit(driver_, items, cfg_, log, **identity_kw):
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
    assert registry.get(jid).status == "stopped"  # 终态改由 registry.get 观察，snapshot 只含进行中


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
    job = registry.get(jid)
    assert ok and job.status == "done" and job.done == 10


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
    assert registry.get(jid).status == "skipped"

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
    job2 = registry.get(jid2)
    assert not ok2 and job2.status == "failed" and "403" in job2.error


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


@pytest.mark.asyncio
async def test_relative_dir_normalized_absolutely_in_ledger(tmp_path, monkeypatch):
    """回归（活体发现）：配置 dir 为相对路径时，from_dict 必须归一化为绝对路径，
    账本 dest_path 才不依赖服务端 CWD（file_state/重下都按字面路径解析）。
    活体那行 dest_path=data/downloads/…/S01E194.mkv 换个 CWD 就校验不到文件。"""
    import os

    from sqlmodel import select

    from backend.database import session_scope
    from backend.models import DownloadRecord

    async def fake_fetch(row, item, cookie_str, ua, *, job_id=None):
        return True, f"{item.name} ok"

    monkeypatch.setattr(dl, "_fetch_one", fake_fetch)
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())
    monkeypatch.chdir(tmp_path)  # 相对 dir 按进程 CWD 解析——正是缺陷的成因
    c = DownloadSettings.from_dict({"dir": "data/downloads-cwdtest"})
    assert os.path.isabs(c.dir)  # from_dict 出口即绝对
    await dl.download_task_files(DlDriver(), [saved("1")], c, task_id=951)
    with session_scope() as s:
        rows = list(s.exec(select(DownloadRecord).where(DownloadRecord.task_id == 951)).all())
    try:
        assert len(rows) == 1
        assert os.path.isabs(rows[0].dest_path), rows[0].dest_path
        assert rows[0].dest_path.replace("\\", "/").endswith("data/downloads-cwdtest/动漫/剧/01.mp4")
    finally:
        with session_scope() as s:
            for r in rows:
                s.delete(r)


@pytest.mark.asyncio
async def test_symlinked_download_root_keeps_literal_path_in_ledger(tmp_path, monkeypatch):
    """回归（b1d156b 同款约束）：归一化必须用 abspath 而非 resolve——
    软链挂载根（如 macOS /tmp→/private/tmp）的字面路径要原样进账本，
    否则 aria2 容器按字面挂载点找不到目录（真机 errorCode 18）。"""
    import os
    from pathlib import Path as _P

    from sqlmodel import select

    from backend.database import session_scope
    from backend.models import DownloadRecord

    real = tmp_path / "real_mount_c"
    real.mkdir()
    link = tmp_path / "mnt_c"
    os.symlink(real, link)

    async def fake_fetch(row, item, cookie_str, ua, *, job_id=None):
        return True, f"{item.name} ok"

    monkeypatch.setattr(dl, "_fetch_one", fake_fetch)
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())
    c = DownloadSettings.from_dict({"dir": str(link / "down")})
    assert c.dir == str(link / "down")  # 已是绝对：字面量原样保留，未被 realpath 改写
    await dl.download_task_files(DlDriver(), [saved("1")], c, task_id=952)
    with session_scope() as s:
        rows = list(s.exec(select(DownloadRecord).where(DownloadRecord.task_id == 952)).all())
    try:
        assert len(rows) == 1
        assert rows[0].dest_path.startswith(str(_P(link, "down", "动漫", "剧"))), rows[0].dest_path
        assert str(real) not in rows[0].dest_path  # 绝不展开成软链目标
    finally:
        with session_scope() as s:
            for r in rows:
                s.delete(r)


@pytest.mark.asyncio
async def test_aria2_submit_dir_keeps_symlink_prefix(tmp_path, monkeypatch):
    """回归（aria2 真机活体发现）：投递的 dir 必须保留用户配置路径的字面量，含符号链接层。

    macOS 宿主的 /tmp 常是指向 /private/tmp 的软链，而 aria2 容器只挂载字面的 /tmp/... 路径；
    resolve() 会把 dir 改写成容器内不存在的 realpath，真机立刻 errorCode 18 失败
    （见 test_download_aria2_live.py 的原始 struct）。abspath 只补绝对、不展开软链。"""
    import os
    from pathlib import Path as _P

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

    real = tmp_path / "real_mount"
    real.mkdir()
    link = tmp_path / "mnt"
    os.symlink(real, link)  # 任何 OS 上都构造出「配置路径带软链」的场景
    monkeypatch.setattr(dl.httpx, "AsyncClient", C)
    monkeypatch.setattr(dl, "aria2_reachable", lambda c: _ret(True))
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())
    c = DownloadSettings(mode="aria2", dir=str(link / "down"), aria2_host_port="http://127.0.0.1:6800")
    await dl.download_task_files(DlDriver(), [saved("1")], c)
    opts = posted[0]["params"][-1]
    assert os.path.isabs(opts["dir"])
    assert opts["dir"] == str(_P(link, "down", "动漫/剧"))  # 字面软链前缀原样投递，未被 realpath 改写


# ---- aria2 投递前的「目标已到位」预检（2026-10-05 活体翻车：重下整片被改名 .1.mkv 重下）----


def _gid_client(posted: list, gid: str = "gid-skip-test"):
    """假 RPC 客户端：记录收到的每一次 addUri 投递，恒返回成功 gid。"""

    class R:
        def json(self):
            return {"result": gid}

    class C:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posted.append(json)
            return R()

    return C


def _rows_for(task_id: int) -> list[DownloadRecord]:
    with session_scope() as s:
        return list(s.exec(select(DownloadRecord).where(DownloadRecord.task_id == task_id)).all())


def _drop_rows(*task_ids: int) -> None:
    with session_scope() as s:
        for r in s.exec(select(DownloadRecord).where(col(DownloadRecord.task_id).in_(task_ids))).all():
            s.delete(r)


def _aria2_skip_driver(monkeypatch):
    """装好「aria2 可达」与假 RPC 客户端，返回投递记录列表。"""
    posted: list = []
    monkeypatch.setattr(dl.httpx, "AsyncClient", _gid_client(posted))
    monkeypatch.setattr(dl, "aria2_reachable", lambda c: _ret(True))
    return posted


PLACEHOLDER_SIZE = 1 << 20  # 1MiB：远大于任何文件系统块，稀疏与实占字节的落差才看得出来


def make_sparse_placeholder(path, size: int = PLACEHOLDER_SIZE):
    """复刻 aria2 `--file-allocation` 的现场：目标声明整片大小，却一个字节数据都没写（全是洞）。

    夹具自检（评审要求）：st_blocks 必须远小于 st_size，否则本机文件系统把块即时补齐了，
    稀疏占位根本没复现出来 —— 这种情况下大声 skip，绝不静默通过。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.truncate(size)
    st = os.stat(path)
    if st.st_blocks * 512 >= st.st_size:
        pytest.skip(
            f"稀疏占位复现失败：本文件系统即时分配了块（st_blocks*512={st.st_blocks * 512} "
            f">= st_size={st.st_size}），「大小相符但没数据」这一现场在此 fs 上不存在"
        )
    return path


class PlaceholderDriver(DlDriver):
    """直链行带真实体积（1MiB）：DlDriver 的 size=10 太小，稀疏占位在块粒度上看不出来。"""

    async def get_download_urls(self, fids):
        self.requested.append(list(fids))
        rows = [
            {"fid": f, "file_name": f, "size": PLACEHOLDER_SIZE, "download_url": f"http://dl/{f}"} for f in fids
        ]
        return rows, "DLCK=1"


@pytest.mark.asyncio
async def test_aria2_existing_file_skips_without_adduri(tmp_path, monkeypatch):
    """活体翻车回归：dest 已是同名同大小的完整文件时，重下绝不投递 addUri。

    daemon 撞名不改写而是自动改名（S01E194.mkv → S01E194.1.mkv）重下整片 3.5GB，
    新行 dest_path 却仍记原路径，对账把原文件误判成本次下载——账本描述了一次从未落到
    记录路径的下载。内置下载器对此短路记 skipped，aria2 模式必须同款语义。"""
    posted = _aria2_skip_driver(monkeypatch)
    dest = tmp_path / "剧" / "S01E194.mkv"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"0" * 10)  # DlDriver 直链行 size=10：同大小 → 已到位
    item = dl.DownloadItem(fid="ep194", name="S01E194.mkv", size=10, local_path=dest)
    c = cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800")
    task_id = 971
    try:
        lines = await dl.download_items(
            DlDriver(), [item], c, log=lambda *a: None,
            task_id=task_id, taskname="凡人", account_id=7, driver_key="fake",
        )
        # 1) 一次 addUri 都没投出去（活体缺陷的直接证据）
        assert [p for p in posted if p["method"] == "aria2.addUri"] == []
        # 2) 摘要行与内置同款：✅ 前缀（通知聚合按它计数）+ 跳过文案
        assert len(lines) == 1 and lines[0].startswith("✅") and "跳过（已存在）" in lines[0]
        assert dest.read_bytes() == b"0" * 10  # 原文件分毫未动
        # 3) 账本恰一行、终态 skipped、字段齐全；ref_id 无 gid 可用 → uuid4 hex 前 12 位（同 registry.create）
        rows = _rows_for(task_id)
        assert len(rows) == 1, [(r.ref_id, r.status) for r in rows]
        r = rows[0]
        assert r.status == "skipped" and r.source == "aria2"
        assert r.dest_path == str(dest) and r.fid == "ep194" and r.taskname == "凡人"
        assert r.driver_key == "fake" and r.account_id == 7 and r.size_total == 10
        assert re.fullmatch(r"[0-9a-f]{12}", r.ref_id) and r.ref_id != "gid-skip-test"
    finally:
        _drop_rows(task_id)


@pytest.mark.asyncio
async def test_aria2_missing_or_wrong_size_still_submits(tmp_path, monkeypatch):
    """预检不许把好路堵死：目标不存在、大小不符、大小未知一律照常投递。

    大小未知（网盘报 0）这一条是评审 Important 3 更正过的断言：批 4f0f2e6 曾按「非空即到位」
    容忍，导致内置模式（要求已知大小才跳）与 aria2 模式（非空就跳）语义分叉，
    残留的半截文件在 aria2 模式下被永久假跳过。现在两种模式同判：没验证过 = 重新下。
    """
    posted = _aria2_skip_driver(monkeypatch)
    gone = tmp_path / "gone.mkv"
    partial = tmp_path / "partial.mkv"
    partial.write_bytes(b"0" * 5)  # size=10 预期却只有 5 字节 → 不符，必须重下
    unknown = tmp_path / "unknown.mkv"
    unknown.write_bytes(b"0" * 7)  # 网盘报 0/未知：无从校验 → 不许跳，必须重下

    class ZeroSizeDriver(DlDriver):
        async def get_download_urls(self, fids):
            rows = [
                {"fid": f, "file_name": f, "size": 0, "download_url": f"http://dl/{f}"} for f in fids
            ]
            return rows, "DLCK=1"

    items = [
        dl.DownloadItem(fid="a-gone", name="gone.mkv", size=10, local_path=gone),
        dl.DownloadItem(fid="b-partial", name="partial.mkv", size=10, local_path=partial),
        dl.DownloadItem(fid="c-unknown", name="unknown.mkv", size=0, local_path=unknown),
    ]
    c = cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800")
    task_id = 972
    try:
        lines = await dl.download_items(
            ZeroSizeDriver(), items, c, log=lambda *a: None, task_id=task_id, driver_key="fake"
        )
        fids = [p["params"][-1]["out"] for p in posted]  # out 即文件名
        assert fids == ["gone.mkv", "partial.mkv", "unknown.mkv"]
        assert not any("跳过（已存在）" in line for line in lines)
        assert all(line.startswith("✅") for line in lines)  # 投递成功也是 ✅，不只看跳过
        assert sorted(r.status for r in _rows_for(task_id)) == ["queued", "queued", "queued"]
    finally:
        _drop_rows(task_id)


@pytest.mark.asyncio
async def test_aria2_preflight_never_skips_preallocated_placeholder(tmp_path, monkeypatch):
    """评审 Critical 1（RED→GREEN）：投递前预检读的必须是真下完的文件，不能是 aria2 的残骸。

    4f0f2e6 的预检只看 `st_size == 预期`，而 aria2 `--file-allocation` 预分配出来的占位大小
    恰好等于整片（活体：3,509,370,877 字节占位 + 1097 字节 .aria2 控制文件）。失败的下载留下
    这种残骸后，每次重下都同样 mint 一条 skipped 行计入「本地下载 N/M」并把垃圾推给 Emby，
    UI 上没有任何动作能修好这个文件。真下完的完整文件仍然要跳（那是 4f0f2e6 的原意）。
    """
    posted = _aria2_skip_driver(monkeypatch)
    sparse = make_sparse_placeholder(tmp_path / "prealloc.mkv")  # 大小对得上，但全是洞
    controlled = tmp_path / "resuming.mkv"  # 字节齐了，但 aria2 还在下（同目录留控制文件）
    controlled.write_bytes(b"0" * PLACEHOLDER_SIZE)
    (tmp_path / "resuming.mkv.aria2").write_bytes(b"0" * 1097)
    complete = tmp_path / "complete.mkv"  # 真下完的：控制文件已被 aria2 收尾删掉
    complete.write_bytes(b"0" * PLACEHOLDER_SIZE)

    items = [
        dl.DownloadItem(fid="a-sparse", name="prealloc.mkv", size=PLACEHOLDER_SIZE, local_path=sparse),
        dl.DownloadItem(fid="b-controlled", name="resuming.mkv", size=PLACEHOLDER_SIZE, local_path=controlled),
        dl.DownloadItem(fid="c-complete", name="complete.mkv", size=PLACEHOLDER_SIZE, local_path=complete),
    ]
    c = cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800")
    task_id = 981
    try:
        lines = await dl.download_items(
            PlaceholderDriver(), items, c, log=lambda *a: None, task_id=task_id, driver_key="fake"
        )
        outs = [p["params"][-1]["out"] for p in posted if p["method"] == "aria2.addUri"]
        assert outs == ["prealloc.mkv", "resuming.mkv"], "残骸必须重新投递，完整文件才允许跳过"
        assert "跳过（已存在）complete.mkv" in lines[2]
        assert not any("跳过" in line for line in lines[:2])
        assert sorted(r.status for r in _rows_for(task_id)) == ["queued", "queued", "skipped"]
    finally:
        _drop_rows(task_id)


@pytest.mark.asyncio
async def test_fetch_one_never_skips_preallocated_placeholder(tmp_path, monkeypatch):
    """Critical 1 的内置侧：_fetch_one 的短路判据必须与 aria2 预检同源（第三处变体也不许留下）。

    内置下载器原来只比 `path.stat().st_size == size`，同一份预分配残骸在内置模式下也会被
    当成「已存在」跳过；重下写完的字节必须让文件真的通过到位校验。
    """
    class Resp:
        status_code = 200

        async def aiter_bytes(self, _n):
            for _ in range(PLACEHOLDER_SIZE // 4096):
                yield b"1" * 4096

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            return Resp()

    monkeypatch.setattr(dl.httpx, "AsyncClient", Client)
    row = {"download_url": "http://dl/1", "size": PLACEHOLDER_SIZE}

    sparse = make_sparse_placeholder(tmp_path / "prealloc.mp4")
    ok, msg = await dl._fetch_one(row, dl.DownloadItem(fid="1", name="prealloc.mp4",
                                                       size=PLACEHOLDER_SIZE, local_path=sparse), "", "UA")
    assert ok and "跳过" not in msg, f"稀疏占位不许当已存在：{msg}"
    assert hist._file_check(str(sparse), PLACEHOLDER_SIZE)[1] is True  # 重下后真的完整了

    controlled = tmp_path / "resuming.mp4"
    controlled.write_bytes(b"0" * PLACEHOLDER_SIZE)
    (tmp_path / "resuming.mp4.aria2").write_bytes(b"0" * 1097)
    ok2, msg2 = await dl._fetch_one(row, dl.DownloadItem(fid="2", name="resuming.mp4",
                                                         size=PLACEHOLDER_SIZE, local_path=controlled), "", "UA")
    assert ok2 and "跳过" not in msg2, f"aria2 控制文件在途不许当已存在：{msg2}"


@pytest.mark.asyncio
async def test_unknown_size_verdict_agrees_across_both_modes(tmp_path, monkeypatch):
    """Important 3：网盘没报大小时两种模式必须同判 —— 都算「没验证过」，重新下。

    旧语义只在 aria2 模式非空即跳（内置要求已知大小），残留半截文件在 aria2 下永久假跳过。
    """
    class Resp:
        status_code = 200

        async def aiter_bytes(self, _n):
            yield b"2" * 7

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    streamed: list = []
    posted: list = []

    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            streamed.append(url)
            return Resp()

        async def post(self, url, json):
            posted.append(json)
            return _GidResp()

    class _GidResp:
        def json(self):
            return {"result": "gid-unknown-size"}

    monkeypatch.setattr(dl.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dl, "aria2_reachable", lambda c: _ret(True))

    builtin_dest = tmp_path / "builtin" / "nolength.mkv"
    builtin_dest.parent.mkdir(parents=True)
    builtin_dest.write_bytes(b"0" * 7)  # 半截残留，且没人知道它该多大
    ok, msg = await dl._fetch_one({"download_url": "http://dl/b", "size": 0},
                                  dl.DownloadItem(fid="b", name="nolength.mkv", size=0, local_path=builtin_dest),
                                  "", "UA")
    assert ok and "跳过" not in msg and streamed == ["http://dl/b"]

    aria_dir = tmp_path / "aria2"
    aria_dir.mkdir()
    aria_dest = aria_dir / "nolength.mkv"
    aria_dest.write_bytes(b"0" * 7)
    task_id = 982
    try:
        lines = await dl.download_items(
            ZeroSizeRowDriver(), [dl.DownloadItem(fid="a", name="nolength.mkv", size=0, local_path=aria_dest)],
            cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800"),
            log=lambda *a: None, task_id=task_id, driver_key="fake",
        )
        assert lines[0].startswith("✅") and "跳过" not in lines[0]
        assert [p["params"][-1]["out"] for p in posted if p["method"] == "aria2.addUri"] == ["nolength.mkv"]
        assert [r.status for r in _rows_for(task_id)] == ["queued"]
    finally:
        _drop_rows(task_id)


class ZeroSizeRowDriver(DlDriver):
    """直链行 size=0（网盘不报体积）：两种模式在这里必须走同一条判据。"""

    async def get_download_urls(self, fids):
        self.requested.append(list(fids))
        rows = [{"fid": f, "file_name": f, "size": 0, "download_url": f"http://dl/{f}"} for f in fids]
        return rows, "DLCK=1"


@pytest.mark.asyncio
async def test_aria2_skipped_row_survives_reconcile(tmp_path, monkeypatch):
    """skipped 是终态：open_records 永不取它，uuid ref_id（非 gid）也绝不会被拿去问 tellStatus。"""
    ref = "a" * 12  # 与 mint 出来的 ref_id 同形：12 位 hex、不是 daemon gid
    dest = tmp_path / "survive.mkv"
    hist.start(
        source="aria2", ref_id=ref, task_id=973, taskname="T", filename="survive.mkv",
        dest_path=str(dest), size_total=10, fid="F", driver_key="fake", account_id=None,
    )
    hist.finish(ref, source="aria2", status="skipped")
    # 钉住机制本身：终态行绝不进 open_records
    assert ref not in {r["ref_id"] for r in hist.open_records()}
    # 真问一次账：混一条同 ref 形态的 open 行（g-live），reconcile 只许问它、不碰 skipped
    open_ref = "b" * 12
    hist.start(
        source="aria2", ref_id=open_ref, task_id=974, taskname="T", filename="live.mkv",
        dest_path=str(tmp_path / "live.mkv"), size_total=10, fid="F", driver_key="fake", account_id=None,
    )
    calls = []

    async def fake_status(_c):
        return []

    async def fake_rpc(_c, method, *params):
        calls.append(params[0])
        return {"result": {"status": "active", "completedLength": "1", "totalLength": "10"}}

    monkeypatch.setattr(dl, "aria2_status", fake_status)
    monkeypatch.setattr(dl, "aria2_rpc", fake_rpc)
    try:
        await hist.reconcile(cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800"))
        assert open_ref in calls and ref not in calls
        row = _rows_for(973)[0]
        assert row.status == "skipped" and row.error == "" and row.size_total == 10
        assert row.finished_at is not None
    finally:
        _drop_rows(973, 974)
