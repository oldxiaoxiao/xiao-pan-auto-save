"""下载账本测试：生命周期写入、对账收口、文件校验、清理。"""

from __future__ import annotations

from sqlmodel import select

from backend.core.download_registry import registry
from backend.database import session_scope
from backend.models import DownloadRecord
from backend.services import download_history as hist
from backend.services import download_service as dl
from backend.tests.test_download import DlDriver, cfg, saved


def _rows(task_id: int | None = None) -> list[DownloadRecord]:
    """按 task_id 取本用例自己的记录，避免依赖测试执行顺序。"""
    with session_scope() as s:
        stmt = select(DownloadRecord).order_by(DownloadRecord.id)
        if task_id is not None:
            stmt = stmt.where(DownloadRecord.task_id == task_id)
        return list(s.exec(stmt).all())


async def test_builtin_flow_writes_one_terminal_record(tmp_path, monkeypatch):
    async def fake_fetch(row, item, cookie_str, ua, *, job_id=None):
        registry.update(job_id, done=row["size"], status="done")
        return True, f"{item.name}（0.0MB）"

    monkeypatch.setattr(dl, "_fetch_one", fake_fetch)
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())

    await dl.download_task_files(
        DlDriver(), [saved("1")], cfg(tmp_path), task_id=701, taskname="追更", account_id=3, driver_key="fake"
    )

    rows = _rows(701)
    assert len(rows) == 1  # start + finish 是同一条，不是两条
    r = rows[0]
    assert (r.status, r.source, r.taskname, r.fid, r.account_id, r.driver_key) == (
        "done", "builtin", "追更", "1", 3, "fake",
    )
    assert r.size_total == 10 and r.size_done == 10 and r.finished_at is not None
    assert r.dest_path.endswith("动漫/剧/01.mp4")
    registry.remove(r.ref_id)


async def test_builtin_failure_records_error(tmp_path, monkeypatch):
    async def boom(row, item, cookie_str, ua, *, job_id=None):
        raise RuntimeError("直链 403")

    monkeypatch.setattr(dl, "_fetch_one", boom)
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())
    await dl.download_task_files(DlDriver(), [saved("x")], cfg(tmp_path), task_id=702, taskname="t")
    r = _rows(702)[-1]
    assert r.status == "failed" and "直链 403" in r.error


async def test_history_write_failure_does_not_break_download(tmp_path, monkeypatch):
    def blow_up(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(hist, "start", blow_up)
    monkeypatch.setattr(dl, "_emby_refresh", lambda c, log: _noop())

    async def fake_fetch(row, item, cookie_str, ua, *, job_id=None):
        registry.update(job_id, status="done")
        return True, "ok"

    monkeypatch.setattr(dl, "_fetch_one", fake_fetch)
    lines = await dl.download_task_files(DlDriver(), [saved("1")], cfg(tmp_path), task_id=703)
    assert lines and lines[0].startswith("✅")


async def test_aria2_submit_records_queued_with_gid(tmp_path, monkeypatch):
    class FakeResp:
        def json(self):
            return {"result": "GID-777"}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            return FakeResp()

    monkeypatch.setattr(dl.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(dl, "aria2_reachable", lambda c: _ret(True))
    c = cfg(tmp_path, mode="aria2", aria2_host_port="http://127.0.0.1:6800")
    await dl.download_task_files(
        DlDriver(), [saved("1")], c, task_id=704, taskname="A", account_id=5, driver_key="fake"
    )

    r = _rows(704)[-1]
    assert (r.status, r.source, r.ref_id, r.taskname) == ("queued", "aria2", "GID-777", "A")


async def test_start_then_finish_updates_single_row():
    hist.start(
        source="builtin", ref_id="r-2", task_id=705, taskname="t", filename="f", dest_path="/d/f",
        size_total=100, fid="F", driver_key="fake", account_id=None,
    )
    hist.finish("r-2", source="builtin", status="skipped", size_done=0, size_total=100, error="已存在")
    rows = _rows(705)
    assert len(rows) == 1 and rows[0].status == "skipped" and rows[0].error == "已存在"
    assert rows[0].finished_at is not None


async def test_finish_unknown_ref_is_noop():
    hist.finish("no-such-ref", source="aria2", status="done")  # 不抛异常
    assert _rows(799) == []


async def test_history_finish_falls_back_when_job_evicted():
    """Task 1 评审边界：registry.get() 为 None（有界 _done 淘汰）时降级兜底，不抛不丢终态。"""
    hist.start(
        source="builtin", ref_id="evicted-1", task_id=706, taskname="t", filename="f", dest_path="/d/f",
        size_total=5, fid="F", driver_key="fake", account_id=None,
    )
    assert registry.get("evicted-1") is None  # 该 ref 不在内存注册表
    warns = []
    dl._history_finish(
        lambda level, msg: warns.append((level, msg)), source="builtin", ref_id="evicted-1", ok=True,
        fallback_name="f",
    )
    r = _rows(706)[0]
    assert r.status == "done" and r.finished_at is not None and not warns  # ok 兜底成 done，未触发告警


async def _noop():
    return None


async def _ret(v):
    return v


def _seed(n: int, *, status: str = "done", task_id: int, filename: str = "a.mp4", dest: str = "/d/a.mp4") -> None:
    ref_base = f"{task_id}-{status}"
    for i in range(n):
        ref = f"{ref_base}-{i}"
        hist.start(
            source="builtin", ref_id=ref, task_id=task_id, taskname="T", filename=filename,
            dest_path=dest, size_total=10, fid=f"F{i}", driver_key="fake", account_id=None,
        )
        if status != "queued":
            hist.finish(ref, source="builtin", status=status, size_done=10)


def test_list_records_filters_and_pages():
    _seed(3, status="done", task_id=801, filename="英雄.mp4")
    _seed(2, status="failed", task_id=802, filename="反派.mkv")

    r = hist.list_records(task_id=801, page=1, page_size=2)
    assert r["total"] == 3 and len(r["items"]) == 2
    r2 = hist.list_records(task_id=801, page=2, page_size=2)
    assert r2["total"] == 3 and len(r2["items"]) == 1
    assert hist.list_records(status="failed", task_id=802)["total"] == 2
    assert hist.list_records(keyword="英雄")["total"] == 3
    assert hist.list_records(keyword="绝无此名")["total"] == 0
    assert all(i["status"] in ("failed", "queued") for i in hist.list_records(status="failed,queued")["items"])


def test_file_state_ok_missing_and_directory(tmp_path):
    good = tmp_path / "in.mp4"
    good.write_bytes(b"x")
    assert hist.file_state(str(good)) == "ok"
    assert hist.file_state(str(tmp_path / "gone.mp4")) == "missing"
    d = tmp_path / "some_dir"
    d.mkdir()
    assert hist.file_state(str(d)) == "unknown"  # 目录不是常规文件


def test_file_state_unknown_on_permission_error(monkeypatch):
    import types

    def boom(path):
        raise PermissionError("挂载抖动")

    # 只替换模块命名空间里的 os（file_state 只用到 os.stat），不碰全局 os
    monkeypatch.setattr(hist, "os", types.SimpleNamespace(stat=boom))
    assert hist.file_state("/anywhere") == "unknown"


def test_get_record_roundtrip():
    _seed(1, status="failed", task_id=803, filename="z.mp4")
    rec = hist.get_record(_id_of_ref("803-failed-0"))
    assert rec["task_id"] == 803 and rec["status"] == "failed" and rec["fid"].startswith("F")
    assert hist.get_record(0) is None


def _id_of_ref(ref: str) -> int:
    with session_scope() as s:
        return int(s.exec(select(DownloadRecord).where(DownloadRecord.ref_id == ref)).first().id)


def test_delete_record_true_and_row_gone():
    _seed(1, status="done", task_id=804)
    rid = _id_of_ref("804-done-0")
    assert hist.delete_record(rid) is True
    assert hist.get_record(rid) is None
    assert hist.delete_record(rid) is False  # 再删一次不存在


