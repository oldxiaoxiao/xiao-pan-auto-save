"""下载账本测试：生命周期写入、对账收口、文件校验、清理。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import col, select

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
    # 关键词断言按 task_id 限定：总表计数随共享测试库增长，不限定就依赖全表状态
    assert hist.list_records(task_id=801, keyword="英雄")["total"] == 3
    assert hist.list_records(keyword="绝无此名")["total"] == 0
    assert all(i["status"] in ("failed", "queued") for i in hist.list_records(status="failed,queued")["items"])


def test_keyword_escapes_like_wildcards():
    _seed(2, status="done", task_id=805, filename="100%.mp4")
    _seed(1, status="failed", task_id=805, filename="100A.mp4")
    # 关键词里的 % 是字面字符：只命中 "100%.mp4" 两条，不得作为通配符把 "100A.mp4" 也算进来
    assert hist.list_records(task_id=805, keyword="100%")["total"] == 2
    assert hist.list_records(task_id=805, keyword="100")["total"] == 3  # 不带通配符的普通子串仍全部命中


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


# ---- reconcile：非终态对账 ----


@pytest.fixture(autouse=True)
def _close_leftover_open_records():
    """共享临时库的隔离夹具：把此前用例遗留的非终态记录先收口。

    否则 open_records 会把旧 queued 一起带进 multicall 的 asked 列表：
    返回按位置对齐时 struct 会错位到别的 gid 上，"calls == []" 断言也会被旧记录触发。
    对账用例只关心自己 seed 的那一条，先清场才能各用例自洽。
    """
    with session_scope() as s:
        rows = s.exec(select(DownloadRecord).where(col(DownloadRecord.status).in_(("queued", "downloading")))).all()
        for row in rows:
            row.status = "stopped"
            row.finished_at = datetime.now()
            s.add(row)


def _seed_open(ref: str, *, source: str = "aria2", dest: str = "/d/x.mkv", size: int = 100) -> int:
    return hist.start(
        source=source, ref_id=ref, task_id=901, taskname="T", filename="x.mkv", dest_path=dest,
        size_total=size, fid="F", driver_key="fake", account_id=None,
    )


def _age(ref: str, hours: float) -> None:
    with session_scope() as s:
        row = s.exec(select(DownloadRecord).where(DownloadRecord.ref_id == ref)).first()
        row.created_at = datetime.now() - timedelta(hours=hours)
        s.add(row)


def _aria2_cfg() -> dl.DownloadSettings:
    return dl.DownloadSettings(mode="aria2", aria2_host_port="http://127.0.0.1:6800")


def _patch_aria2(monkeypatch, *, active: list[dict], struct=None, structs: dict[str, dict | None] | None = None):
    """active：aria2_status 返回的进行中 job（用 id 标识 gid）。

    struct：所有被问 gid 共用的单条 tellDownloadResult（None 表示 gid 已被 aria2 丢弃）；
    structs：多 gid 用例按 gid 分别指定结果，值为 None 同样表示被丢弃。
    返回条目从代码实际发出的 multicall 参数里读出 gid、按请求顺序逐条构造：
    若实现把 asked 与 result 的 zip 写反或逆序，错配会直接反映到断言上，而不是被固定单条返回掩盖。
    """
    calls: list[tuple] = []

    async def fake_status(cfg):
        return active

    async def fake_rpc(cfg, method, *params):
        calls.append((method, params))
        asked = [c["params"][0] for c in params[0]]  # multicall 每个子调用只带一个 gid
        entries = []
        for gid in asked:
            s = struct if structs is None else structs[gid]  # KeyError 即代码问了预期外的 gid
            entries.append({"errorMessage": f"{gid} not found"} if s is None else {"result": [s]})
        return {"result": entries}

    monkeypatch.setattr(dl, "aria2_status", fake_status)
    monkeypatch.setattr(dl, "aria2_rpc", fake_rpc)
    return calls


def _status_of(ref: str) -> DownloadRecord:
    with session_scope() as s:
        return s.exec(select(DownloadRecord).where(DownloadRecord.ref_id == ref)).first()


async def test_reconcile_aria2_complete_marks_done(tmp_path, monkeypatch):
    _seed_open("g-ok", dest=str(tmp_path / "x.mkv"))
    _patch_aria2(monkeypatch, active=[], struct={"status": "complete", "completedLength": "100", "totalLength": "100"})
    await hist.reconcile(_aria2_cfg())
    r = _status_of("g-ok")
    assert r.status == "done" and r.size_done == 100 and r.finished_at is not None


async def test_reconcile_aria2_error_marks_failed_with_message(tmp_path, monkeypatch):
    _seed_open("g-err", dest=str(tmp_path / "y.mkv"))
    _patch_aria2(monkeypatch, active=[], struct={"status": "error", "error_message": "直链过期", "completedLength": "10"})
    await hist.reconcile(_aria2_cfg())
    r = _status_of("g-err")
    assert r.status == "failed" and r.error == "直链过期"


async def test_reconcile_gid_still_running_keeps_queued(tmp_path, monkeypatch):
    _seed_open("g-run", dest=str(tmp_path / "z.mkv"))
    calls = _patch_aria2(monkeypatch, active=[{"id": "g-run", "status": "downloading"}], struct=None)
    await hist.reconcile(_aria2_cfg())
    assert _status_of("g-run").status == "queued"
    assert calls == []  # 仍在跑的不该去问 tellDownloadResult


async def test_reconcile_falls_back_to_file_stat(tmp_path, monkeypatch):
    dest = tmp_path / "already.mkv"
    dest.write_bytes(b"0" * 100)
    _seed_open("g-file", dest=str(dest))
    _patch_aria2(monkeypatch, active=[], struct=None)  # gid 已被 aria2 丢弃
    await hist.reconcile(_aria2_cfg())
    r = _status_of("g-file")
    assert r.status == "done" and r.size_done == 100


async def test_reconcile_partial_file_stays_queued_until_stale(tmp_path, monkeypatch):
    dest = tmp_path / "half.mkv"
    dest.write_bytes(b"0" * 50)  # 大小不符，不能算完成
    _seed_open("g-half", dest=str(dest))
    _patch_aria2(monkeypatch, active=[], struct=None)
    await hist.reconcile(_aria2_cfg())
    assert _status_of("g-half").status == "queued"


async def test_reconcile_builtin_orphan_young_keeps_queued(tmp_path, monkeypatch):
    """内置 job 随进程重启消失、文件也不在：不足 24h 先不动。"""
    _seed_open("b-new", source="builtin", dest=str(tmp_path / "none.mkv"))
    _patch_aria2(monkeypatch, active=[], struct=None)
    await hist.reconcile(_aria2_cfg())
    assert _status_of("b-new").status == "queued"


async def test_reconcile_builtin_orphan_stale_marks_failed(tmp_path, monkeypatch):
    _seed_open("b-old", source="builtin", dest=str(tmp_path / "none2.mkv"))
    _age("b-old", 25)
    _patch_aria2(monkeypatch, active=[], struct=None)
    await hist.reconcile(_aria2_cfg())
    r = _status_of("b-old")
    assert r.status == "failed" and "对账超时" in r.error


async def test_reconcile_adopts_builtin_terminal_from_registry(tmp_path, monkeypatch):
    """内存 registry 已有终态但库里还挂着：以 registry 为准补写（finish 漏写时自愈）。"""
    jid = registry.create(task_id=901, taskname="T", filename="f.mkv", dest_path="/d/f.mkv", total=10)
    _seed_open(jid, source="builtin")
    registry.update(jid, done=10, status="stopped", error="已停止")
    _patch_aria2(monkeypatch, active=[], struct=None)
    await hist.reconcile(_aria2_cfg())
    assert _status_of(jid).status == "stopped"
    registry.remove(jid)


async def test_reconcile_skips_aria2_when_mode_is_builtin(tmp_path, monkeypatch):
    """内置模式下不该去敲 aria2 RPC（cfg.mode 非 aria2 时直接走兜底）。"""
    _seed_open("g-off", source="aria2", dest=str(tmp_path / "off.mkv"))
    calls = _patch_aria2(monkeypatch, active=[], struct={"status": "complete"})
    await hist.reconcile(dl.DownloadSettings(mode="builtin"))
    assert calls == []
    assert _status_of("g-off").status == "queued"  # 既没问 RPC 也没文件，先挂着


async def test_reconcile_multicall_aligns_results_per_asked_gid(tmp_path, monkeypatch):
    """一次问两个开放 gid：第一个已被 aria2 丢弃（errorMessage 条目）、第二个有 complete 结果。

    返回按代码实际请求的 gid 顺序逐条构造；zip 操作数写反或 asked 逆序都会让两条互串结局
    （被丢弃的错标 done、有结果的悬挂 queued），本用例即可抓住。
    """
    _seed_open("g-drop", dest=str(tmp_path / "drop.mkv"))  # 文件不在：只能落 24h 兜底
    _seed_open("g-full", dest=str(tmp_path / "full.mkv"))
    _patch_aria2(monkeypatch, active=[], structs={
        "g-drop": None,
        "g-full": {"status": "complete", "completedLength": "100", "totalLength": "100"},
    })
    await hist.reconcile(_aria2_cfg())
    assert _status_of("g-drop").status == "queued"  # 各归各：走文件兜底，不足 24h 先挂着
    r = _status_of("g-full")
    assert r.status == "done" and r.size_done == 100 and r.finished_at is not None


async def test_reconcile_aria2_error_field_accepts_both_spellings(tmp_path, monkeypatch):
    """tellDownloadResult 结构体里错误字段两种拼写都要认：snake_case error_message（aria2 惯例）
    与 camelCase errorMessage（与封装层一致）；两者皆无才落通用文案。"""
    _seed_open("g-snake", dest=str(tmp_path / "s.mkv"))
    _seed_open("g-camel", dest=str(tmp_path / "c.mkv"))
    _seed_open("g-bare", dest=str(tmp_path / "b.mkv"))
    _patch_aria2(monkeypatch, active=[], structs={
        "g-snake": {"status": "error", "error_message": "直链过期", "completedLength": "10"},
        "g-camel": {"status": "error", "errorMessage": "种子不足", "completedLength": "5"},
        "g-bare": {"status": "error", "completedLength": "0"},
    })
    await hist.reconcile(_aria2_cfg())
    for ref in ("g-snake", "g-camel", "g-bare"):
        assert _status_of(ref).status == "failed"
    assert _status_of("g-snake").error == "直链过期"
    assert _status_of("g-camel").error == "种子不足"  # 不能退化成通用文案
    assert _status_of("g-bare").error == "aria2 未成功"


async def test_reconcile_file_fallback_records_real_size_when_total_zero(tmp_path, monkeypatch):
    """size_total=0 时"非空即到位"，done 要写 stat 到的真实字节数，不能留 size_done=0。"""
    dest = tmp_path / "nolength.bin"
    dest.write_bytes(b"0" * 37)
    _seed_open("g-nosize", dest=str(dest), size=0)
    _patch_aria2(monkeypatch, active=[], struct=None)
    await hist.reconcile(_aria2_cfg())
    r = _status_of("g-nosize")
    assert r.status == "done" and r.size_done == 37 and r.size_total == 37


