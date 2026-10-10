"""FR-07 下载断点续传与中断收口。

内置下载过去的进度只活在内存 registry 里：进程一重启，3GB 下到 90% 也得从头再来。
本轮把进度挪到两个能扛过重启的地方——磁盘上的 `.part`（续传凭据）与账本状态（可继续）。
"""

from __future__ import annotations

import pytest
from sqlmodel import delete

from backend.database import session_scope
from backend.models import DownloadRecord
from backend.services import download_history as hist
from backend.services import download_service as dl


@pytest.fixture(autouse=True)
def clean_db():
    with session_scope() as s:
        s.exec(delete(DownloadRecord))
    yield
    with session_scope() as s:
        s.exec(delete(DownloadRecord))


def _patch_http(monkeypatch, *, code: int, chunks: list[bytes]):
    """替换 httpx.AsyncClient，返回实际发出的 headers 供断言。"""
    seen: dict = {}

    class Resp:
        status_code = code

        async def aiter_bytes(self, _n):
            for c in chunks:
                yield c

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Client:
        def __init__(self, **kw):  # noqa: ARG002
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None):  # noqa: ARG002
            seen["headers"] = dict(headers or {})
            return Resp()

    monkeypatch.setattr(dl.httpx, "AsyncClient", Client)
    return seen


# ------------------------------------------------------------ 续传偏移


def test_resume_offset_uses_part_size(tmp_path):
    part = tmp_path / "a.mkv.part"
    part.write_bytes(b"x" * 40)
    assert dl._resume_offset(part, 100) == 40


def test_resume_offset_zero_when_size_unknown(tmp_path):
    """大小未知时不能续：无从判断 part 是否完整，续了可能写出坏文件。"""
    part = tmp_path / "a.mkv.part"
    part.write_bytes(b"x" * 40)
    assert dl._resume_offset(part, 0) == 0
    assert part.exists(), "判不了就别删，留着至少不丢已有字节之外的信息"


def test_resume_offset_drops_oversized_part(tmp_path):
    """part 比目标还大 = 上次写完没改名的残留，留着会让续传写出超长文件。"""
    part = tmp_path / "a.mkv.part"
    part.write_bytes(b"x" * 200)
    assert dl._resume_offset(part, 100) == 0
    assert not part.exists()


def test_resume_hint_reports_progress(tmp_path):
    part = tmp_path / "a.mkv.part"
    part.write_bytes(b"x" * 250)
    assert "25%" in dl._resume_hint(part, 1000)
    assert "无法续传" in dl._resume_hint(part, 0)


# ------------------------------------------------------------ 断点续传


@pytest.mark.asyncio
async def test_206_continues_from_part(tmp_path, monkeypatch):
    """服务端认 Range：追加写，最终文件 = 已下 + 新下。"""
    seen = _patch_http(monkeypatch, code=206, chunks=[b"z" * 20])
    target = tmp_path / "a.mkv"
    (tmp_path / "a.mkv.part").write_bytes(b"x" * 10)
    item = dl.DownloadItem(fid="f", name="a.mkv", size=30, local_path=target)

    ok, msg = await dl._fetch_one({"download_url": "http://x", "size": 30}, item, "", "UA")

    assert ok is True, msg
    assert seen["headers"].get("range") == "bytes=10-"
    assert target.read_bytes() == b"x" * 10 + b"z" * 20
    assert "续传自" in msg, msg
    assert not (tmp_path / "a.mkv.part").exists(), "完成后 part 必须改名消失"


@pytest.mark.asyncio
async def test_200_means_server_ignored_range(tmp_path, monkeypatch):
    """服务端忽略 Range 返回完整资源：必须截断重写，叠加必然写坏。"""
    seen = _patch_http(monkeypatch, code=200, chunks=[b"z" * 30])
    target = tmp_path / "a.mkv"
    (tmp_path / "a.mkv.part").write_bytes(b"x" * 10)
    item = dl.DownloadItem(fid="f", name="a.mkv", size=30, local_path=target)

    ok, msg = await dl._fetch_one({"download_url": "http://x", "size": 30}, item, "", "UA")

    assert ok is True, msg
    assert seen["headers"].get("range") == "bytes=10-", "仍然要发，给服务端一次机会"
    assert target.read_bytes() == b"z" * 30, "不能把旧 part 留在前面"
    assert "续传自" not in msg


@pytest.mark.asyncio
async def test_416_discards_stale_part(tmp_path, monkeypatch):
    """Range 不满足（part 比资源大，多半资源换过）：删掉重来。"""
    _patch_http(monkeypatch, code=416, chunks=[b"z" * 30])
    target = tmp_path / "a.mkv"
    (tmp_path / "a.mkv.part").write_bytes(b"x" * 99)
    item = dl.DownloadItem(fid="f", name="a.mkv", size=30, local_path=target)

    ok, msg = await dl._fetch_one({"download_url": "http://x", "size": 30}, item, "", "UA")

    assert ok is True, msg
    assert target.read_bytes() == b"z" * 30


@pytest.mark.asyncio
async def test_short_read_keeps_part_for_next_try(tmp_path, monkeypatch):
    """流断了也要保留 part：下次从断处继续，而不是每次从头。"""
    _patch_http(monkeypatch, code=206, chunks=[b"z" * 5])
    target = tmp_path / "a.mkv"
    part = tmp_path / "a.mkv.part"
    part.write_bytes(b"x" * 10)
    item = dl.DownloadItem(fid="f", name="a.mkv", size=30, local_path=target)

    ok, msg = await dl._fetch_one({"download_url": "http://x", "size": 30}, item, "", "UA")

    assert ok is False
    assert "可继续" in msg, msg
    assert part.stat().st_size == 15, "已下的字节必须留住"
    assert not target.exists()


# ------------------------------------------------------------ 中断收口


def test_mark_interrupted_records_progress(tmp_path):
    """重启后：builtin 在途行收口为 interrupted，并把 .part 的进度写进账本。"""
    dest = tmp_path / "b.mkv"
    (tmp_path / "b.mkv.part").write_bytes(b"x" * 400)
    rid = hist.start(
        source="builtin", ref_id="r1", task_id=None, taskname="剧", filename="b.mkv",
        dest_path=str(dest), size_total=1000, fid="F", driver_key="fake", account_id=None,
    )

    assert hist.mark_interrupted() == 1

    with session_scope() as s:
        row = s.get(DownloadRecord, rid)
        assert row.status == "interrupted"
        assert row.size_done == 400
        assert "40%" in row.error and "可从断点继续" in row.error
        assert row.finished_at is not None
    # 收口后不再算"在途"：重下入口必须放行，否则用户干等 24h
    assert hist.open_records() == []
    assert hist.has_open_for_path(str(dest)) is False


def test_mark_interrupted_leaves_aria2_alone(tmp_path):
    """aria2 自己有持久化与续传：抢先判"中断"会盖掉它真实的在途状态。"""
    dest = tmp_path / "c.mkv"
    rid = hist.start(
        source="aria2", ref_id="g1", task_id=None, taskname="剧", filename="c.mkv",
        dest_path=str(dest), size_total=1000, fid="F", driver_key="fake", account_id=None,
    )
    assert hist.mark_interrupted() == 0
    with session_scope() as s:
        assert s.get(DownloadRecord, rid).status == "queued"


def test_interrupted_is_terminal_so_it_can_be_retried(tmp_path):
    """interrupted 必须算终态：否则它既不能重下，又要等 24h 才被判失败。"""
    assert "interrupted" in hist.TERMINAL
    dest = tmp_path / "d.mkv"
    rid = hist.start(
        source="builtin", ref_id="r2", task_id=None, taskname="剧", filename="d.mkv",
        dest_path=str(dest), size_total=1000, fid="F", driver_key="fake", account_id=None,
    )
    hist.mark_interrupted()
    # 终态才能被保留期清理扫到，也才能让 list/has_open 判定放行
    assert hist.has_open_for_path(str(dest)) is False
    with session_scope() as s:
        assert s.get(DownloadRecord, rid).status == "interrupted"
