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
        DlDriver(), [saved("1")], cfg(tmp_path), task_id=701, taskname="追更", account_id=3
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
        DlDriver(), [saved("1")], c, task_id=704, taskname="A", account_id=5
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
    # job 不在时体积传 None 而非 0：size_total 必须保持 start 落的 5，账本不被淘汰场景抹平
    assert r.status == "done" and r.size_total == 5 and r.size_done == 0
    assert r.finished_at is not None and not warns  # ok 兜底成 done，未触发告警


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

    否则 open_records 会把旧 queued 一起带进对账询问的 gid 集合：
    fake 按下到的 gid 返回结果，预期外的 gid 会 KeyError；"calls == []" 断言也会被旧记录触发。
    对账用例只关心自己 seed 的那几条，先清场才能各用例自洽。
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

    struct：所有被问 gid 共用的单条 aria2.tellStatus 结果；
    structs：多 gid 用例按 gid 分别指定结果，值为 None 表示真机查不到该 gid
            （aria2 丢弃结果后 RPC 返回 JSON-RPC error 体，不是异常——真机实测）。
    真机没有 tellDownloadResult，也没有可用的 system.multicall（前导 token 会被拒），
    所以这里按被问的那个 gid 返回它自己的结果体：实现若把某个 gid 的 struct 安到别的行上，
    对应行的断言会直接失败。方法名不认识时同样返回真机那种 error 体，让实现走兜底而不是抛异常。
    """
    calls: list[tuple] = []

    async def fake_status(cfg):
        return active

    async def fake_rpc(cfg, method, *params):
        calls.append((method, params))
        if method != "aria2.tellStatus":
            return {"error": {"code": 1, "message": f"No such method: {method.removeprefix('aria2.')}"}}
        gid = params[0]  # token 由 aria2_rpc 统一前置，业务函数收到的是裸 gid
        s = struct if structs is None else structs[gid]  # KeyError 即代码问了预期外的 gid
        return {"result": s} if s is not None else {"error": {"code": 1, "message": f"gid {gid} 不存在"}}

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


async def test_reconcile_aria2_failure_reason_is_backfilled(tmp_path, monkeypatch):
    """真机失败作业的理由字段是 camelCase errorMessage（实测），要原样进账本供排查。"""
    dest = tmp_path / "y.mkv"
    _seed_open("g-err", dest=str(dest))
    _patch_aria2(monkeypatch, active=[], structs={
        "g-err": {"status": "error", "errorCode": "22", "errorMessage": "直链过期", "completedLength": "10"},
    })
    await hist.reconcile(_aria2_cfg())
    r = _status_of("g-err")
    assert r.status == "failed" and r.error == "直链过期"
    assert not dest.exists()  # 文件从没落地：只能是 RPC 判出来的 failed


async def test_reconcile_aria2_removed_marks_failed(tmp_path, monkeypatch):
    _seed_open("g-gone", dest=str(tmp_path / "r.mkv"))
    _patch_aria2(monkeypatch, active=[], struct={"status": "removed", "completedLength": "0"})
    await hist.reconcile(_aria2_cfg())
    assert _status_of("g-gone").status == "failed"


async def test_reconcile_tellstatus_requests_lean_keys(tmp_path, monkeypatch):
    """tellStatus 不传 keys 会把 files[] 整块带回来（真机实测几百条 uri），对账用不上：必须显式列字段。"""
    _seed_open("g-keys", dest=str(tmp_path / "k.mkv"))
    calls = _patch_aria2(monkeypatch, active=[], struct={"status": "complete", "completedLength": "100",
                                                          "totalLength": "100"})
    await hist.reconcile(_aria2_cfg())
    assert [m for m, _ in calls] == ["aria2.tellStatus"]  # 不再有 system.multicall / tellDownloadResult
    gid, keys = calls[0][1]
    assert gid == "g-keys"
    assert "files" not in keys
    assert set(keys) >= {"gid", "status", "totalLength", "completedLength", "errorCode", "errorMessage"}


async def test_reconcile_non_terminal_aria2_status_leaves_row_alone(tmp_path, monkeypatch):
    """tellStatus 回 active/waiting/paused 时不许收口：即便文件已同大小在原地，也只能由进行中语义展示。"""
    dest = tmp_path / "still-running.mkv"
    dest.write_bytes(b"0" * 100)
    for ref in ("g-active", "g-wait", "g-pause"):
        _seed_open(ref, dest=str(dest))
    _patch_aria2(monkeypatch, active=[], structs={
        "g-active": {"status": "active", "completedLength": "1", "totalLength": "100"},
        "g-wait": {"status": "waiting", "completedLength": "0", "totalLength": "100"},
        "g-pause": {"status": "paused", "completedLength": "0", "totalLength": "100"},
    })
    await hist.reconcile(_aria2_cfg())
    for ref in ("g-active", "g-wait", "g-pause"):
        assert _status_of(ref).status == "queued"


async def test_reconcile_gid_still_running_keeps_queued(tmp_path, monkeypatch):
    _seed_open("g-run", dest=str(tmp_path / "z.mkv"))
    calls = _patch_aria2(monkeypatch, active=[{"id": "g-run", "status": "downloading"}], struct=None)
    await hist.reconcile(_aria2_cfg())
    assert _status_of("g-run").status == "queued"
    assert calls == []  # 仍在跑的不该去问 tellStatus


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


async def test_reconcile_applies_each_gid_its_own_struct(tmp_path, monkeypatch):
    """交错三个开放 gid：complete / error / 已被 aria2 丢弃，各行的结局只能来自它自己那条 struct。

    per-gid 调用把"按位置 zip"这个错配来源消掉了，但换了个错配面：结果按 gid 归档时必须
    认得回同一行。g-drop 的文件不在、也不足 24h，只能保持 queued——若 struct 互串，
    它会错标 done 或 g-full 会悬挂。
    """
    _seed_open("g-drop", dest=str(tmp_path / "drop.mkv"))
    _seed_open("g-full", dest=str(tmp_path / "full.mkv"))
    _seed_open("g-bad", dest=str(tmp_path / "bad.mkv"))
    _patch_aria2(monkeypatch, active=[], structs={
        "g-drop": None,
        "g-full": {"status": "complete", "completedLength": "100", "totalLength": "100"},
        "g-bad": {"status": "error", "errorCode": "2", "errorMessage": "没有可用的直链", "completedLength": "0"},
    })
    await hist.reconcile(_aria2_cfg())
    assert _status_of("g-drop").status == "queued"  # 各归各：走文件兜底，不足 24h 先挂着
    r = _status_of("g-full")
    assert r.status == "done" and r.size_done == 100 and r.finished_at is not None and not r.error
    bad = _status_of("g-bad")
    assert bad.status == "failed" and bad.error == "没有可用的直链"


async def test_reconcile_aria2_error_field_accepts_both_spellings(tmp_path, monkeypatch):
    """结果体里错误理由两种拼写都要认：camelCase errorMessage（真机实测字段）优先，
    snake_case error_message 兼容；两者皆空但有非零 errorCode 时把码带上（可诊断），
    连码都没有才落通用文案。"""
    _seed_open("g-camel", dest=str(tmp_path / "c.mkv"))
    _seed_open("g-snake", dest=str(tmp_path / "s.mkv"))
    _seed_open("g-code", dest=str(tmp_path / "e.mkv"))
    _seed_open("g-bare", dest=str(tmp_path / "b.mkv"))
    _patch_aria2(monkeypatch, active=[], structs={
        "g-camel": {"status": "error", "errorMessage": "种子不足", "completedLength": "5"},
        "g-snake": {"status": "error", "error_message": "直链过期", "completedLength": "10"},
        "g-code": {"status": "error", "errorCode": "22", "errorMessage": "", "completedLength": "0"},
        "g-bare": {"status": "error", "completedLength": "0"},
    })
    await hist.reconcile(_aria2_cfg())
    for ref in ("g-camel", "g-snake", "g-code", "g-bare"):
        assert _status_of(ref).status == "failed"
    assert _status_of("g-camel").error == "种子不足"  # 不能退化成通用文案
    assert _status_of("g-snake").error == "直链过期"
    assert "22" in _status_of("g-code").error  # 理由为空也要能从行里查出是哪个 aria2 错误码
    assert _status_of("g-bare").error == "aria2 未成功"


async def test_reconcile_unreachable_daemon_raises_nothing(tmp_path, monkeypatch):
    """daemon 不可达时逐 gid 的异常必须被咽下：对账跑在历史查询的 HTTP 请求里，不能冒泡。"""
    _seed_open("g-dead", dest=str(tmp_path / "orphan.mkv"))

    async def boom_status(cfg):
        raise OSError("connection refused")

    async def boom_rpc(cfg, method, *params):
        raise OSError("connection refused")

    monkeypatch.setattr(dl, "aria2_status", boom_status)
    monkeypatch.setattr(dl, "aria2_rpc", boom_rpc)
    await hist.reconcile(_aria2_cfg())  # 不抛
    assert _status_of("g-dead").status == "queued"  # 既问不到也没文件：先挂着，等 24h 兜底


async def test_reconcile_file_fallback_records_real_size_when_total_zero(tmp_path, monkeypatch):
    """size_total=0 时"非空即到位"，done 要写 stat 到的真实字节数，不能留 size_done=0。"""
    dest = tmp_path / "nolength.bin"
    dest.write_bytes(b"0" * 37)
    _seed_open("g-nosize", dest=str(dest), size=0)
    _patch_aria2(monkeypatch, active=[], struct=None)
    await hist.reconcile(_aria2_cfg())
    r = _status_of("g-nosize")
    assert r.status == "done" and r.size_done == 37 and r.size_total == 37


# ---- retry_record：单文件重下 ----


def _register_fake_driver(monkeypatch) -> None:
    """把测试驱动 DlDriver 临时挂进注册表，让 retry_record 能按 driver_key="fake" 解析到它。

    注册表只扫描 backend/drivers/ 下的模块，测试文件里的假驱动不在其中；用 setitem 而非
    直接写 DRIVERS，用例结束自动还原，不会污染 test_router 的 supported 集合断言。
    """
    from backend.drivers import DRIVERS

    monkeypatch.setitem(DRIVERS, "fake", DlDriver)


class _Acc:
    id, cookie, sort_order, driver_key = 1, "ck", 0, "fake"


async def test_retry_record_keeps_original_dest_path(tmp_path, monkeypatch):
    """重下必须打回原目标路径，不能被 resolve_local 按网盘目录重算。"""
    dest = str(tmp_path / "外部目录" / "已存在.mkv")
    rid = hist.start(
        source="builtin", ref_id="retry-src", task_id=921, taskname="T", filename="已存在.mkv",
        dest_path=dest, size_total=10, fid="FID1", driver_key="fake", account_id=None,
    )
    hist.finish("retry-src", source="builtin", status="failed", error="HTTP 500")
    seen = []

    async def fake_items(driver, items, cfg, *, log, task_id=None, taskname="", account_id=None, driver_key=""):
        seen.append((items[0].fid, str(items[0].local_path), taskname, account_id))
        return [f"✅ {items[0].name}"]

    _register_fake_driver(monkeypatch)
    monkeypatch.setattr(dl, "download_items", fake_items)
    monkeypatch.setattr(dl, "_account_for", lambda rec: _Acc())
    await dl.retry_record(hist.get_record(rid), dl.DownloadSettings(dir=str(tmp_path)), log=lambda *a, **k: None)
    assert seen == [("FID1", dest, "T", 1)]


async def test_retry_without_account_logs_and_returns(tmp_path, monkeypatch):
    rid = hist.start(
        source="builtin", ref_id="retry-noacc", task_id=923, taskname="t", filename="a", dest_path="/d/a",
        size_total=1, fid="F", driver_key="fake", account_id=None,
    )
    calls = []

    async def fake_items(*a, **k):
        calls.append(1)
        return []

    _register_fake_driver(monkeypatch)
    monkeypatch.setattr(dl, "download_items", fake_items)
    monkeypatch.setattr(dl, "_account_for", lambda rec: None)
    msgs = []
    await dl.retry_record(hist.get_record(rid), dl.DownloadSettings(), log=lambda lvl, m: msgs.append(m))
    assert calls == [] and any("账号" in m for m in msgs)


async def test_retry_with_unsupported_driver_logs_and_returns(tmp_path, monkeypatch):
    from backend.drivers import get_driver_class

    rid = hist.start(
        source="builtin", ref_id="retry-nodrv", task_id=924, taskname="t", filename="a", dest_path="/d/a",
        size_total=1, fid="F", driver_key="no_such_drive", account_id=None,
    )
    calls = []

    async def fake_items(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(dl, "download_items", fake_items)
    assert get_driver_class("no_such_drive") is None
    msgs = []
    await dl.retry_record(hist.get_record(rid), dl.DownloadSettings(), log=lambda lvl, m: msgs.append(m))
    assert calls == [] and any("驱动" in m for m in msgs)


async def test_retry_download_items_exception_is_logged_not_raised(tmp_path, monkeypatch):
    """后台重下里 download_items 炸出（如失效分享的陈旧 fid 抛 DriveError）必须落日志、不上抛。

    retry_record 由路由层 create_task 裸调度，不设防则异常只出现在 uvicorn stderr
    （"Task exception was never retrieved"），日志 tab 与账本两头无痕，而 UI 已提示"重下已开始"。
    与首下路径 task_service._download_for_task 的兜底对齐；取直链失败本身仍不落账本行。
    """
    from backend.drivers.base import DriveError

    rid = hist.start(
        source="builtin", ref_id="retry-boom", task_id=927, taskname="追更", filename="a.mkv",
        dest_path=str(tmp_path / "a.mkv"), size_total=1, fid="F", driver_key="fake", account_id=None,
    )
    hist.finish("retry-boom", source="builtin", status="failed", error="HTTP 500")
    entered = []

    async def boom(driver, items, cfg, *, log, **kw):
        entered.append(1)
        raise DriveError("分享已失效")

    _register_fake_driver(monkeypatch)
    monkeypatch.setattr(dl, "download_items", boom)
    monkeypatch.setattr(dl, "_account_for", lambda rec: _Acc())
    msgs = []
    await dl.retry_record(  # 不抛异常：await 正常返回即证明异常没有上抛
        hist.get_record(rid), dl.DownloadSettings(dir=str(tmp_path)), log=lambda lvl, m: msgs.append((lvl, m))
    )
    assert entered == [1]  # 确实走到了下载这一步
    assert any(lvl == "error" and "追更" in m and "重下异常" in m and "分享已失效" in m for lvl, m in msgs)
    assert len(_rows(927)) == 1  # 只有一条旧记录：炸在写入点之前，不新增账本行


# ---- prune：保留策略与清理 ----


def _seed_finished(ref: str, *, status: str = "done", age_days: int = 0) -> int:
    rid = hist.start(
        source="builtin", ref_id=ref, task_id=911, taskname="t", filename=ref, dest_path=f"/d/{ref}",
        size_total=1, fid="F", driver_key="fake", account_id=None,
    )
    hist.finish(ref, source="builtin", status=status, size_done=1)
    if age_days:
        with session_scope() as s:
            row = s.get(DownloadRecord, rid)
            row.finished_at = datetime.now() - timedelta(days=age_days)
            s.add(row)
    return rid


def _has(ref: str) -> bool:
    return any(r.ref_id == ref for r in _rows())


def test_prune_auto_uses_retention_days():
    _seed_finished("p-old", age_days=120)
    _seed_finished("p-new", age_days=1)
    hist.start(  # 非终态记录不受自动清理影响
        source="builtin", ref_id="p-open", task_id=911, taskname="t", filename="p-open", dest_path="/d/p-open",
        size_total=1, fid="F", driver_key="fake", account_id=None,
    )
    assert hist.prune("auto", "days_90") >= 1
    assert not _has("p-old") and _has("p-new") and _has("p-open")


def test_prune_forever_removes_nothing():
    _seed_finished("pf-old", age_days=400)
    assert hist.prune("auto", "forever") == 0
    assert _has("pf-old")


def test_prune_failed_only_removes_failed():
    _seed_finished("x-done", status="done")
    _seed_finished("x-fail", status="failed")
    assert hist.prune("failed") >= 1
    assert _has("x-done") and not _has("x-fail")


def test_prune_all_removes_records():
    _seed_finished("y-done")
    assert hist.prune("all") >= 1
    assert not _has("y-done")


def test_retention_days_parsing_table():
    """保留策略解析：只有 days_<整数> 生效，脏值（含 None/空串/非数字/小数）一律当作「不清」。

    days_0 / days_-5 归到 1 天：宁可当晚就清，也不要因为一个 0 变成「永久」或 0 天全删。
    days_1_2 解析成 12 —— int() 认 PEP 515 的数字分隔符，这是既有语义，不是脏值分支，钉住它。
    """
    cases = [
        ("days_30", 30),
        ("days_90", 90),
        ("days_180", 180),
        ("days_365", 365),
        ("days_1", 1),
        ("days_0", 1),
        ("days_-5", 1),
        ("days_1_2", 12),
        ("days_abc", None),
        ("days_", None),
        ("days_90x", None),
        ("days_1.5", None),
        ("days_1e3", None),
        ("forever", None),
        ("", None),
        (None, None),
        ("  days_30", None),
        ("DAYS_30", None),
    ]
    for retention, expected in cases:
        assert hist._retention_days(retention) == expected, f"retention={retention!r}"


def test_prune_with_corrupt_retention_deletes_nothing():
    """脏保留值不能把清理变成「全删」：解析失败 → 天数 None → auto 分支一条都不动。"""
    _seed_finished("pc-old", age_days=400)
    assert hist.prune("auto", "days_abc") == 0
    assert _has("pc-old")
