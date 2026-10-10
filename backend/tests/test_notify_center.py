"""FR-04 通知分级与免打扰。

钉死三件事：
1. 分级——需处理与仅告知分成两条，且需处理先发；
2. 免打扰——需处理事件只攒不丢，窗口结束后合成一条摘要补发；
3. 按任务静音——只关仅告知，需处理永远发得出去。

时间一律显式传入（`now=`）：挂钟几点跑测试不能影响结论。
"""

from datetime import datetime

import pytest
from sqlmodel import select

from backend.database import session_scope
from backend.models import NotifyPending, Task
from backend.services import notify_center as nc


@pytest.fixture(autouse=True)
def _clean_queue():
    yield
    with session_scope() as session:
        for row in session.exec(select(NotifyPending)).all():
            session.delete(row)


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as c:
        yield c


def _settings(quiet: bool = False, start: str = "23:00", end: str = "08:00") -> dict:
    return {
        "notify_enabled": True,
        "notify_quiet": {"enabled": quiet, "start": start, "end": end},
    }


def _spy():
    sent: list[tuple[str, str]] = []

    async def send(title, content, push_config, settings, log):
        sent.append((title, content))

    return sent, send


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 2, hour, minute)


# ------------------------------------------------------------ 免打扰窗口判定


def test_quiet_window_wraps_over_midnight():
    """23:00–08:00 是跨天窗口：夜里在窗口内，早上八点整出窗口。"""
    cfg = _settings(True)
    assert nc.in_quiet(_at(23, 30), cfg) is True
    assert nc.in_quiet(_at(0, 10), cfg) is True
    assert nc.in_quiet(_at(7, 59), cfg) is True
    assert nc.in_quiet(_at(8, 0), cfg) is False
    assert nc.in_quiet(_at(12, 0), cfg) is False


def test_quiet_window_same_day_and_disabled():
    cfg = _settings(True, start="01:00", end="06:00")
    assert nc.in_quiet(_at(2, 0), cfg) is True
    assert nc.in_quiet(_at(9, 0), cfg) is False
    # 起止相同等于"整天免打扰"，按关闭处理，不许把通知静默掐死
    assert nc.in_quiet(_at(2, 0), _settings(True, start="08:00", end="08:00")) is False
    assert nc.in_quiet(_at(23, 30), _settings(False)) is False


def test_quiet_config_falls_back_on_garbage():
    """配置脏了（非 dict / 缺键）按默认窗口算，不能因为读不懂就永久静音或永久不静音。"""
    cfg = nc.quiet_config({"notify_quiet": "oops"})
    assert cfg == {"enabled": True, "start": "23:00", "end": "08:00"}
    assert nc.quiet_config({})["enabled"] is True


# ------------------------------------------------------------ 分级


@pytest.mark.asyncio
async def test_dispatch_splits_levels_into_two_messages():
    sent, send = _spy()
    await nc.dispatch(
        [nc.NotifyLine("❌《剧》链接失效", level=nc.LEVEL_ACTION), nc.NotifyLine("✅《剧》添加追更", level=nc.LEVEL_INFO)],
        settings=_settings(),
        push_config={},
        log=None,
        send=send,
        title="运行结果",
    )
    assert len(sent) == 2
    assert "【需处理】" in sent[0][0] and "链接失效" in sent[0][1]
    assert "【仅告知】" in sent[1][0] and "添加追更" in sent[1][1]
    assert "添加追更" not in sent[0][1], "级别混在一条里就等于没分级"


def test_classify_fallback_for_untagged_lines():
    assert nc.classify("❌《剧》：没有支持该链接的网盘驱动") == nc.LEVEL_ACTION
    assert nc.classify("磁盘空间不足：需要 11 GB") == nc.LEVEL_ACTION
    assert nc.classify("✅《剧》添加追更") == nc.LEVEL_INFO
    assert nc.classify("⏸️ 本次跳过 1 个已停用任务") == nc.LEVEL_INFO


def test_notify_line_keeps_being_a_string():
    """NotifyLine 是 str 子类：历史代码里 join / startswith / in 比较都不能因为加了级别而失效。"""
    one = nc.line("✅《剧》添加追更", nc.LEVEL_INFO, task_id=7)
    assert one == "✅《剧》添加追更"
    assert "\n".join([one, "x"]) == "✅《剧》添加追更\nx"
    assert one.startswith("✅")
    assert one.level == nc.LEVEL_INFO and one.task_id == 7


# ------------------------------------------------------------ 免打扰：只攒不丢


@pytest.mark.asyncio
async def test_action_events_are_queued_not_dropped_during_quiet_hours():
    """Given 免打扰时段内产生 3 条需处理事件，Then 一条都不发、全部入队。"""
    sent, send = _spy()
    result = await nc.dispatch(
        [(nc.LEVEL_ACTION, f"❌《剧{i}》链接失效") for i in range(1, 4)],
        settings=_settings(True),
        push_config={},
        log=None,
        send=send,
        title="运行结果",
        now=_at(23, 30),
    )
    assert sent == []
    assert result["queued"] == 3 and result["quiet"] is True
    assert nc.pending_count() == 3
    assert nc.pending_count(nc.LEVEL_ACTION) == 3


@pytest.mark.asyncio
async def test_digest_is_sent_once_after_quiet_hours():
    """When 免打扰结束，Then 收到 1 条聚合摘要，包含 3 条事件。"""
    sent, send = _spy()
    await nc.dispatch(
        [(nc.LEVEL_ACTION, f"❌《剧{i}》链接失效") for i in range(1, 4)],
        settings=_settings(True),
        push_config={},
        log=None,
        send=send,
        title="运行结果",
        now=_at(23, 30),
    )
    sent.clear()

    flushed = await nc.flush_pending(settings=_settings(True), push_config={}, log=None, send=send, now=_at(8, 30))
    assert flushed == 3
    assert nc.pending_count() == 0, "补发后队列必须清空，否则会重复打扰"
    assert len(sent) == 1
    title, body = sent[0]
    assert "免打扰摘要" in title and "共 3 条" in title
    for i in range(1, 4):
        assert f"《剧{i}》链接失效" in body


@pytest.mark.asyncio
async def test_digest_is_not_sent_while_still_quiet():
    """还在窗口内就别发：摘要作业提前触发（或跨天边界）时不能提前打扰。"""
    nc.enqueue(nc.LEVEL_ACTION, "❌《剧》链接失效", title="运行结果")
    sent, send = _spy()
    assert await nc.flush_pending(settings=_settings(True), push_config={}, log=None, send=send, now=_at(3, 0)) == 0
    assert sent == []
    assert nc.pending_count() == 1


@pytest.mark.asyncio
async def test_dispatch_flushes_pending_before_current_batch():
    """出窗口后的第一次通知：先把攒的补发，再发本次，顺序不能反。"""
    nc.enqueue(nc.LEVEL_ACTION, "❌《旧》链接失效", title="运行结果")
    sent, send = _spy()
    await nc.dispatch(
        [nc.NotifyLine("✅《新》添加追更", level=nc.LEVEL_INFO)],
        settings=_settings(True),
        push_config={},
        log=None,
        send=send,
        title="运行结果",
        now=_at(9, 0),
    )
    assert len(sent) == 2
    assert "《旧》链接失效" in sent[0][1]
    assert "【仅告知】" in sent[1][0]
    assert nc.pending_count() == 0


@pytest.mark.asyncio
async def test_notify_disabled_sends_nothing_and_keeps_queue():
    """全局通知关掉时不发也不清队：队列是"欠用户的账"，不能因为暂时关掉就销账。"""
    nc.enqueue(nc.LEVEL_ACTION, "❌《剧》链接失效")
    sent, send = _spy()
    off = _settings()
    off["notify_enabled"] = False
    assert await nc.flush_pending(settings=off, push_config={}, log=None, send=send, now=_at(9, 0)) == 0
    assert sent == []
    assert nc.pending_count() == 1


@pytest.mark.asyncio
async def test_digest_collapses_info_flood():
    """半夜攒了 20 条转存成功，早上不该被一堵墙砸到：只列前 5 条 + 条数。"""
    for i in range(20):
        nc.enqueue(nc.LEVEL_INFO, f"✅《剧{i}》添加追更")
    nc.enqueue(nc.LEVEL_ACTION, "❌《剧X》链接失效")
    sent, send = _spy()
    await nc.flush_pending(settings=_settings(True), push_config={}, log=None, send=send, force=True)
    body = sent[0][1]
    assert "《剧X》链接失效" in body
    assert "《剧0》添加追更" in body and "《剧19》添加追更" not in body
    assert "另有 15 条告知类消息" in body


# ------------------------------------------------------------ 按任务关闭「仅告知」


def _mk_task(**kw) -> int:
    with session_scope() as s:
        t = Task(taskname=kw.pop("taskname", "剧"), shareurl="https://x/s", savepath="/s", **kw)
        s.add(t)
        s.commit()
        s.refresh(t)
        return int(t.id or 0)


@pytest.mark.asyncio
async def test_muted_task_drops_info_but_keeps_action():
    """Given 用户关掉任务 B 的仅告知，When B 转存成功，Then 不推告知；B 失效时仍需处理。"""
    muted = _mk_task(taskname="静音剧", notify_info=False)
    loud = _mk_task(taskname="正常剧", notify_info=True)

    sent, send = _spy()
    await nc.dispatch(
        [
            nc.NotifyLine("✅《静音剧》添加追更", level=nc.LEVEL_INFO, task_id=muted),
            nc.NotifyLine("✅《正常剧》添加追更", level=nc.LEVEL_INFO, task_id=loud),
        ],
        settings=_settings(),
        push_config={},
        log=None,
        send=send,
        title="运行结果",
    )
    assert len(sent) == 1 and "静音剧" not in sent[0][1]
    assert "正常剧" in sent[0][1]

    sent.clear()
    await nc.dispatch(
        [nc.NotifyLine("❌《静音剧》链接失效", level=nc.LEVEL_ACTION, task_id=muted)],
        settings=_settings(),
        push_config={},
        log=None,
        send=send,
        title="运行结果",
    )
    assert len(sent) == 1 and "链接失效" in sent[0][1]

    with session_scope() as s:
        for tid in (muted, loud):
            row = s.get(Task, tid)
            if row:
                s.delete(row)


def test_task_notify_info_default_is_on():
    """回归：模型默认值必须是 True。_auto_add_columns 补列取的就是它——
    若变成 False，升级后所有存量任务都会被静音，转存成功的消息一夜之间全没了。"""
    col = Task.__table__.columns["notify_info"]
    py_default = getattr(col.default, "arg", None) if col.default is not None else None
    assert py_default is True


# ------------------------------------------------------------ 设置与接口


def test_quiet_settings_round_trip(client):
    """免打扰要能存能读，且部分写入不能把窗口写残（缺的键回落默认）。"""
    saved = client.put("/api/settings/notify_quiet", json={"value": {"enabled": True, "start": "23:30"}}).json()
    assert saved["value"]["start"] == "23:30"
    assert saved["value"]["end"] == "08:00", "缺的键必须回落默认，不能写成一个整天免打扰"

    back = client.get("/api/settings").json()["notify_quiet"]
    assert back == {"enabled": True, "start": "23:30", "end": "08:00"}

    client.put("/api/settings/notify_quiet", json={"value": {"enabled": False, "start": "23:00", "end": "08:00"}})


def test_pending_endpoint_reports_queue(client):
    """前端要显示"攒了几条"，这个数必须与队列真实内容一致。"""
    nc.enqueue(nc.LEVEL_ACTION, "❌《剧》链接失效", title="运行结果")
    nc.enqueue(nc.LEVEL_INFO, "✅《剧》添加追更", title="运行结果")
    data = client.get("/api/settings/notify/pending").json()
    assert data["ok"] is True and data["count"] == 2
    assert data["action"] == 1 and data["info"] == 1
    assert data["config"]["end"] == "08:00"

    # 立即补发：用户点了按钮就要现在看，不受当前是否在窗口内影响
    flushed = client.post("/api/settings/notify/flush").json()
    assert flushed["ok"] is True and flushed["sent"] == 2
    assert nc.pending_count() == 0
