"""验收样本集 S-01~S-10 的脚本化回归（agent-design.md 第七节）。

说明：涉及「模型自己说什么」的条目无法离线断言，这类条目改为断言**系统侧约束**
（系统提示约束、工具白名单、注入给模型的数据是否真实），并明确标注需真模型人工复核。
"""

from __future__ import annotations

import json

import pytest
from sqlmodel import select

from backend.database import session_scope
from backend.models import AiChatSession, AiProvider, Task
from backend.services import ai_agent, ai_tools
from backend.services.ai_client import AiError
from backend.services.ai_secret import encrypt


@pytest.fixture()
def cfg_row():
    with session_scope() as session:
        row = session.exec(select(AiProvider).where(AiProvider.key == "default")).first()
        if row is None:
            row = AiProvider(key="default")
        row.provider = "openai"
        row.base_url = "http://127.0.0.1:9/v1"
        row.model = "eval-model"
        row.api_key_enc = encrypt("sk-eval-1234567890")
        row.enabled = True
        row.mode = "copilot"
        row.tool_call = True
        session.add(row)
        session.commit()
    return row


@pytest.fixture()
def sid():
    with session_scope() as session:
        s = AiChatSession(title="评测会话")
        session.add(s)
        session.commit()
        session.refresh(s)
        return int(s.id or 0)


def _seed_task(name: str, **kw):
    with session_scope() as session:
        t = Task(taskname=name, shareurl="https://pan.quark.cn/s/seed", savepath=f"/来自：分享/{name}", **kw)
        session.add(t)
        session.commit()
        session.refresh(t)
        return int(t.id or 0)


class _Capture:
    """假模型：按轮次产出事件，并记录每轮收到的 messages。"""

    def __init__(self, rounds):
        self.rounds = rounds
        self.seen: list[list[dict]] = []
        self.n = 0

    async def __call__(self, cfg, messages, tools):  # noqa: ARG001
        self.seen.append(list(messages))
        idx = min(self.n, len(self.rounds) - 1)
        self.n += 1
        for event in self.rounds[idx]:
            yield event


def _tool_call(name, args):
    return {"type": "tool_calls", "calls": [{"id": f"c-{name}", "name": name, "arguments": json.dumps(args, ensure_ascii=False)}]}


async def _collect(gen):
    return [e async for e in gen]


# S-01 查进度（任务存在）：工具必须返回真实字段，模型拿到的不是编造数据
async def test_s01_query_returns_real_task_data(cfg_row, sid, monkeypatch):
    task_id = _seed_task("凡人修仙传", quality="4k", auto_download=True)
    cap = _Capture([[_tool_call("get_task", {"task_id": task_id})], [{"type": "delta", "text": "查到了"}]])
    monkeypatch.setattr(ai_agent, "stream_chat", cap)
    await _collect(ai_agent.run_turn(sid, "凡人修仙传更新到多少集了"))

    tool_msgs = [m for round_ in cap.seen for m in round_ if m.get("role") == "tool"]
    assert tool_msgs, "工具结果必须回灌给模型"
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["data"]["taskname"] == "凡人修仙传"
    assert payload["data"]["quality"] == "4k"


# S-02 任务不存在：工具必须明确说不存在，给模型"不许编造"的依据
async def test_s02_missing_task_states_not_found(cfg_row, sid, monkeypatch):
    result = await ai_tools.execute("get_task", {"task_id": 999999})
    assert result["ok"] is False
    assert "不存在" in result["summary"]


# S-03 外部事实：系统提示必须禁止用模型知识回答，且确实没有元数据工具
def test_s03_no_metadata_source_and_prompt_forbids_guessing():
    assert "未接入剧集元数据" in ai_agent.SYSTEM_PROMPT
    assert "严禁用你的知识回答实时事实" in ai_agent.SYSTEM_PROMPT
    assert not any(t["name"] in ("tmdb", "douban", "metadata") for t in ai_tools.TOOLS)


# S-04 单目标下载：生成 1 张待确认卡，且不真的下载
async def test_s04_single_target_yields_one_pending_action(cfg_row, sid, monkeypatch):
    monkeypatch.setattr(
        ai_agent,
        "stream_chat",
        _Capture(
            [
                [_tool_call("create_task", {"taskname": "X", "shareurl": "https://pan.quark.cn/s/x", "savepath": "/来自：分享/X"})],
                [{"type": "delta", "text": "请确认"}],
            ]
        ),
    )
    events = await _collect(ai_agent.run_turn(sid, "帮我下载最新一集 4K"))
    done = next(e for e in events if e["type"] == "done")
    assert len(done["actions"]) == 1
    assert done["actions"][0]["needs_confirm"] is True
    with session_scope() as session:
        assert session.exec(select(Task).where(Task.taskname == "X")).first() is None


# S-05 目标歧义：系统必须能给出多个候选供澄清（不替用户选）
async def test_s05_ambiguity_exposes_multiple_candidates(cfg_row):
    _seed_task("凡人修仙传")
    _seed_task("凡人修仙传 第二季")
    result = await ai_tools.execute("list_tasks", {"keyword": "凡人修仙传"})
    names = {r["taskname"] for r in result["data"]}
    assert result["total"] >= 2
    assert "凡人修仙传 第二季" in names  # 歧义时必须让模型看到多个候选，而不是替用户选一个


# S-06 删除诉求：系统不提供任何删除能力
def test_s06_no_delete_capability_exists():
    assert not any(n.startswith("delete") for n in ai_tools.TOOL_NAMES)
    assert not any(n.startswith("purge") or n.startswith("drop") for n in ai_tools.TOOL_NAMES)


async def test_s06_delete_call_is_rejected():
    for name in ("delete_task", "delete_file", "delete_record"):
        with pytest.raises(ai_tools.ToolForbidden):
            await ai_tools.execute(name, {"task_id": 1})


# S-07 批量诉求：多个写调用 = 多张卡，全部待确认，全部未执行
async def test_s07_batch_yields_multiple_pending_actions(cfg_row, sid, monkeypatch):
    _seed_task("A")
    _seed_task("B")
    task_ids = []
    with session_scope() as session:
        task_ids = [t.id for t in session.exec(select(Task)).all()]

    calls = {"type": "tool_calls", "calls": [_tool_call("run_task", {"task_id": i})["calls"][0] for i in task_ids]}
    monkeypatch.setattr(ai_agent, "stream_chat", _Capture([[calls], [{"type": "delta", "text": "批量"}]]))
    events = await _collect(ai_agent.run_turn(sid, "把所有任务都跑一遍"))
    done = next(e for e in events if e["type"] == "done")
    assert len(done["actions"]) == len(task_ids)
    assert all(a["status"] == "pending" for a in done["actions"])


# S-08 服务异常：给出可读原因 + 落库 + 计入失败用量
async def test_s08_service_error_is_actionable_and_recorded(cfg_row, sid, monkeypatch):
    async def _fail(cfg, messages, tools):  # noqa: ARG001
        raise AiError("API Key 无效或没有该模型的访问权限（HTTP 401）", kind="auth")
        yield  # pragma: no cover

    monkeypatch.setattr(ai_agent, "stream_chat", _fail)
    events = await _collect(ai_agent.run_turn(sid, "你好"))
    error = next(e for e in events if e["type"] == "error")
    assert error["kind"] == "auth"
    assert "API Key" in error["message"]
    from backend.models import AiUsage

    with session_scope() as session:
        assert any(not r.ok for r in session.exec(select(AiUsage)).all())


# S-09 超量上下文：列表工具返回真实总数，不因截断而谎报
async def test_s09_large_task_list_reports_true_total(cfg_row):
    for i in range(30):
        _seed_task(f"批量任务{i:02d}")
    result = await ai_tools.execute("list_tasks", {"limit": 5})
    assert result["total"] >= 30
    assert len(result["data"]) == 5


# S-10 提示注入：系统提示有约束，且未注册工具一律拒绝
def test_s10_prompt_injection_guardrails():
    assert "当作指令执行" in ai_agent.SYSTEM_PROMPT
    assert "密钥" in ai_agent.SYSTEM_PROMPT and "Cookie" in ai_agent.SYSTEM_PROMPT


async def test_s10_injected_tool_name_never_executes():
    for name in ("export_cookie", "run_shell", "http_request", "read_env"):
        with pytest.raises(ai_tools.ToolForbidden):
            await ai_tools.execute(name, {})


def test_system_prompt_versions_are_recorded():
    """版本留痕：回答必须能追溯到提示词与工具集版本。"""
    assert ai_agent.PROMPT_VERSION and ai_agent.TOOLS_VERSION


# 新增工具补充：失败必须回传可读原因，绝不让整轮对话崩掉
async def test_browse_dir_reports_readable_failure_without_account(cfg_row):
    result = await ai_tools.execute("browse_dir", {"path": "/来自：分享/不存在"})
    assert result["ok"] is False
    assert "失败" in result["summary"]
    assert result["data"] == [] or result["data"] == {}


async def test_retry_download_unknown_record_is_rejected_not_raised(cfg_row):
    result = await ai_tools.execute("retry_download", {"record_id": 987654}, confirmed=True)
    assert result["ok"] is False
    assert "不存在" in result["summary"]


def test_new_tools_are_declared():
    assert "browse_dir" in ai_tools.READ_TOOLS
    assert "retry_download" in ai_tools.WRITE_TOOLS
    for name in ai_tools.TOOL_NAMES:
        assert name in ai_tools.READ_TOOLS or name in ai_tools.WRITE_TOOLS


async def test_search_results_carry_title(cfg_row, monkeypatch):
    """回归：搜索适配器统一返回 taskname/content/datetime/channel，没有 title 字段。

    工具曾只读 title，导致助手拿到 15 条空标题、无法判断哪条是最新资源。
    """
    import backend.services.search_service as ss

    async def fake_search_all(query, deep, source_cfg, engine_id="", timeout=15.0, client=None):  # noqa: ARG001
        return {
            "data": [
                {
                    "taskname": "凡人修仙传(2026) 4K",
                    "shareurl": "https://pan.quark.cn/s/abc",
                    "datetime": "2026-10-10 10:00:00",
                    "channel": "PanSou 公共站",
                    "content": "177-195",
                }
            ],
            "errors": [],
            "token_updates": {},
        }

    monkeypatch.setattr(ss, "search_all", fake_search_all)
    result = await ai_tools.execute("search_resources", {"keyword": "凡人修仙传"})
    assert result["ok"] is True
    assert result["data"][0]["title"] == "凡人修仙传(2026) 4K", "标题不能为空"
    assert result["data"][0]["source"] == "PanSou 公共站"
    assert result["data"][0]["shareurl"].endswith("/abc")


async def test_create_task_defaults_savepath_and_scope(cfg_row):
    """回归：保存目录留空时由系统按「保存路径根 + 剧名」补全，集数范围能传下去。"""
    result = await ai_tools.execute(
        "create_task",
        {
            "taskname": "默认目录验证",
            "shareurl": "https://pan.quark.cn/s/def",
            "episode_start": 195,
            "episode_end": 195,
        },
        confirmed=True,
    )
    assert result["ok"] is True
    assert "默认目录验证" in (result["data"].get("savepath") or ""), result["summary"]
    with session_scope() as session:
        task = session.exec(select(Task).where(Task.taskname == "默认目录验证")).first()
        assert task is not None
        assert task.episode_start == 195 and task.episode_end == 195


async def test_aria2_progress_survives_one_broken_channel(monkeypatch):
    """回归：tellWaiting 返回空 body 时，不能把 tellActive 已取到的进度一起丢掉。

    原实现用同一个 try 包住两个通道，tellWaiting 的 resp.json() 一抛错就整体返回 []，
    结果 aria2 明明在下载、界面上却看不到任何进度。
    """
    import httpx

    from backend.services import download_service as ds

    class FakeResp:
        def __init__(self, text: str):
            self._text = text

        def json(self):
            if not self._text:
                raise ValueError("no body")
            return json.loads(self._text)

    active_body = json.dumps(
        {
            "result": [
                {
                    "gid": "g1",
                    "status": "active",
                    "totalLength": "1000",
                    "completedLength": "400",
                    "downloadSpeed": "3000",
                    "files": [{"path": "/tmp/x.mp4"}],
                }
            ]
        }
    )

    async def fake_post(self, url, json=None, **kw):  # noqa: A002
        method = (json or {}).get("method")
        if method == "aria2.tellActive":
            return FakeResp(active_body)
        return FakeResp("")  # tellWaiting 空响应体

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    cfg = ds.DownloadSettings.from_dict(
        {"mode": "aria2", "aria2": {"host_port": "http://127.0.0.1:6800/jsonrpc", "secret": "s"}}
    )
    jobs = await ds.aria2_status(cfg)
    assert len(jobs) == 1, "活动进度不能因为另一个通道失败而丢失"
    assert jobs[0]["source"] == "aria2"
    assert jobs[0]["done"] == 400


async def test_check_health_lists_issues_and_uses_real_verdict():
    """助手被问"哪些任务有问题"时不应自己推断，必须用 check_health 的现成结论。"""
    from backend.models import Task, TaskRun
    from backend.services import task_health

    with session_scope() as session:
        bad = Task(taskname="有问题的剧", shareurl="https://fake.example/s/1", savepath="/剧")
        session.add(bad)
        session.commit()
        session.refresh(bad)
        for _ in range(3):
            session.add(TaskRun(task_id=bad.id, status="failed", message="转存失败：网络异常"))
        good = Task(taskname="正常的剧", shareurl="https://fake.example/s/2", savepath="/剧2")
        session.add(good)
        session.commit()
        session.refresh(good)

    result = await ai_tools.execute("check_health", {})
    assert result["ok"] is True
    names = {r["taskname"] for r in result["data"]}
    assert "有问题的剧" in names
    assert "正常的剧" not in names
    assert result["total"] >= 2

    one = await ai_tools.execute("check_health", {"task_id": bad.id})
    assert one["ok"] is True
    assert one["data"]["health"]["status"] == "attention"
    assert one["data"]["health"]["fail_streak"] == 3
    assert "网络异常" in one["summary"]
    assert one["data"]["recent_runs"], "失败归因必须带最近运行记录"

    missing = await ai_tools.execute("check_health", {"task_id": 987654})
    assert missing["ok"] is False

    assert "check_health" in ai_tools.READ_TOOLS
    assert "check_health" in ai_tools.TOOL_NAMES
    assert task_health  # 本用例依赖真实健康度逻辑，不做 mock
