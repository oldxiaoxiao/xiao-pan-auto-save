"""对话助手的行为契约测试：写操作必须经确认，凭据不得回显，工具白名单必须生效。"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlmodel import select

from backend.database import session_scope
from backend.models import AiChatMessage, AiChatSession, AiProvider, Task
from backend.services import ai_agent, ai_tools
from backend.services.ai_client import AiError
from backend.services.ai_secret import decrypt, encrypt, mask


@pytest.fixture()
def provider_cfg():
    """写入一份可用的 AI 配置（Key 明文只走加密入口，落库即密文）。"""
    with session_scope() as session:
        row = session.exec(select(AiProvider).where(AiProvider.key == "default")).first()
        if row is None:
            row = AiProvider(key="default")
        row.provider = "openai"
        row.base_url = "https://example.invalid/v1"
        row.model = "test-model"
        row.api_key_enc = encrypt("sk-test-1234567890")
        row.enabled = True
        row.mode = "copilot"
        row.tool_call = True
        session.add(row)
        session.commit()
    return row


@pytest.fixture()
def session_id():
    with session_scope() as session:
        s = AiChatSession(title="测试会话")
        session.add(s)
        session.commit()
        session.refresh(s)
        return int(s.id or 0)


def _fake_stream(events_per_round):
    """按轮次返回预设事件：第一轮出工具调用，第二轮出文本。"""
    state = {"round": 0}

    async def _stream(cfg, messages, tools):  # noqa: ARG001
        idx = min(state["round"], len(events_per_round) - 1)
        state["round"] += 1
        for event in events_per_round[idx]:
            yield event

    return _stream


async def _collect(gen):
    out = []
    async for event in gen:
        out.append(event)
    return out


async def test_write_tool_turns_into_pending_action(provider_cfg, session_id, monkeypatch):
    """模型想建任务：只能变成待确认动作卡，绝不能真的建出来。"""
    monkeypatch.setattr(
        ai_agent,
        "stream_chat",
        _fake_stream(
            [
                [
                    {
                        "type": "tool_calls",
                        "calls": [
                            {
                                "id": "call-1",
                                "name": "create_task",
                                "arguments": json.dumps(
                                    {
                                        "taskname": "凡人修仙传",
                                        "shareurl": "https://pan.quark.cn/s/abc",
                                        "savepath": "/来自：分享/凡人修仙传",
                                    }
                                ),
                            }
                        ],
                    }
                ],
                [{"type": "delta", "text": "我准备好了下面的操作，请你确认后我再执行："}],
            ]
        ),
    )
    events = await _collect(ai_agent.run_turn(session_id, "帮我下载最新一集"))
    done = next(e for e in events if e["type"] == "done")

    assert len(done["actions"]) == 1
    assert done["actions"][0]["kind"] == "create_task"
    assert done["actions"][0]["needs_confirm"] is True
    assert done["actions"][0]["status"] == "pending"
    with session_scope() as session:
        assert session.exec(select(Task).where(Task.taskname == "凡人修仙传")).first() is None


async def test_action_execute_only_after_confirm(provider_cfg, session_id, monkeypatch):
    """确认后执行才真正建任务，且执行结果写回动作卡。"""
    monkeypatch.setattr(
        ai_agent,
        "stream_chat",
        _fake_stream(
            [
                [
                    {
                        "type": "tool_calls",
                        "calls": [
                            {
                                "id": "call-1",
                                "name": "create_task",
                                "arguments": json.dumps(
                                    {
                                        "taskname": "确认后建的任务",
                                        "shareurl": "https://pan.quark.cn/s/xyz",
                                        "savepath": "/来自：分享/确认后建的任务",
                                    }
                                ),
                            }
                        ],
                    }
                ],
                [{"type": "delta", "text": "已生成动作卡"}],
            ]
        ),
    )
    events = await _collect(ai_agent.run_turn(session_id, "建个任务"))
    action = next(e for e in events if e["type"] == "done")["actions"][0]

    result = await ai_agent.run_action(action["id"])
    assert result["ok"] is True
    with session_scope() as session:
        assert session.exec(select(Task).where(Task.taskname == "确认后建的任务")).first() is not None
        msg = session.get(AiChatMessage, next(e for e in events if e["type"] == "done")["message_id"])
        stored = json.loads(msg.actions)
        assert stored[0]["status"] == "done"

    # 重复执行必须被拒，避免一条动作卡产生两次副作用
    again = await ai_agent.run_action(action["id"])
    assert again["ok"] is False


async def test_read_tool_never_returns_cookie(provider_cfg, session_id, monkeypatch):
    """账号工具只给状态摘要，任何情况下都不含 Cookie。"""
    monkeypatch.setattr(
        ai_agent,
        "stream_chat",
        _fake_stream(
            [
                [
                    {
                        "type": "tool_calls",
                        "calls": [{"id": "c1", "name": "list_accounts", "arguments": "{}"}],
                    }
                ],
                [{"type": "delta", "text": "账号如下"}],
            ]
        ),
    )
    events = await _collect(ai_agent.run_turn(session_id, "看看账号"))
    tool_events = [e for e in events if e["type"] == "tool"]
    assert any(e["status"] == "done" for e in tool_events)
    result = await ai_tools.execute("list_accounts", {})
    assert "cookie" not in json.dumps(result, ensure_ascii=False).lower()


async def test_forbidden_tool_name_rejected(provider_cfg):
    """模型幻觉或注入尝试调用未注册工具：直接拒绝，不执行。"""
    with pytest.raises(ai_tools.ToolForbidden):
        await ai_tools.execute("delete_task", {"task_id": 1})
    with pytest.raises(ai_tools.ToolForbidden):
        await ai_tools.execute("execute_sql", {"sql": "delete from task"})


async def test_missing_config_reports_actionable_error(session_id, monkeypatch):
    """未配置模型时，必须给出可操作提示，且不得调用任何模型服务。"""
    with session_scope() as session:
        row = session.exec(select(AiProvider).where(AiProvider.key == "default")).first()
        if row is not None:
            session.delete(row)
    called = {"n": 0}

    async def _boom(cfg, messages, tools):  # noqa: ARG001
        called["n"] += 1
        yield {"type": "delta", "text": "不该被调用"}

    monkeypatch.setattr(ai_agent, "stream_chat", _boom)
    events = await _collect(ai_agent.run_turn(session_id, "你好"))
    assert events[0]["type"] == "error"
    assert "设置" in events[0]["message"]
    assert called["n"] == 0


async def test_model_error_is_persisted_and_surfaced(provider_cfg, session_id, monkeypatch):
    async def _fail(cfg, messages, tools):  # noqa: ARG001
        raise AiError("API Key 无效或没有该模型的访问权限（HTTP 401）", kind="auth")
        yield  # pragma: no cover

    monkeypatch.setattr(ai_agent, "stream_chat", _fail)
    events = await _collect(ai_agent.run_turn(session_id, "你好"))
    error = next(e for e in events if e["type"] == "error")
    assert error["kind"] == "auth"
    with session_scope() as session:
        rows = session.exec(select(AiChatMessage).where(AiChatMessage.session_id == session_id)).all()
        assert any(r.error for r in rows)


def test_secret_roundtrip_and_mask():
    token = encrypt("sk-live-abcdefghijklmnop")
    assert "sk-live" not in token
    assert decrypt(token) == "sk-live-abcdefghijklmnop"
    masked = mask("sk-live-abcdefghijklmnop")
    assert "abcdefghijklmnop" not in masked
    assert masked.startswith("sk-l")


async def test_run_task_settles_in_background(provider_cfg, session_id, monkeypatch):
    """运行任务是长操作：必须立即返回 started，跑完再回写状态，且不能重复触发。"""
    monkeypatch.setattr(
        ai_agent,
        "stream_chat",
        _fake_stream(
            [
                [
                    {
                        "type": "tool_calls",
                        "calls": [
                            {"id": "c1", "name": "run_task", "arguments": json.dumps({"task_id": 1})}
                        ],
                    }
                ],
                [{"type": "delta", "text": "已生成动作卡"}],
            ]
        ),
    )
    events = await _collect(ai_agent.run_turn(session_id, "把这个任务跑一遍"))
    action = next(e for e in events if e["type"] == "done")["actions"][0]

    async def _fake_execute(name, args, *, confirmed=False):  # noqa: ARG001
        return {"ok": True, "summary": "运行结束：无新增", "data": {}, "sources": []}

    monkeypatch.setattr(ai_agent, "execute", _fake_execute)

    result = await ai_agent.run_action(action["id"])
    assert result.get("started") is True
    _, act = ai_agent.find_action(action["id"])
    assert act["status"] == "running"

    # 运行中再次点击不得触发第二次
    again = await ai_agent.run_action(action["id"])
    assert again["ok"] is False

    await asyncio.sleep(0.1)
    _, done = ai_agent.find_action(action["id"])
    assert done["status"] == "done"
    assert "运行结束" in done["result"]


async def test_action_card_prefills_savepath(provider_cfg, session_id, monkeypatch):
    """动作卡预览就必须显示完整保存目录——留空时按「保存路径根 + 剧名」补全。

    只在执行阶段补会让用户看到一张 savepath 为空的卡片，无法判断资源会存到哪。
    """
    monkeypatch.setattr(
        ai_agent,
        "stream_chat",
        _fake_stream(
            [
                [
                    {
                        "type": "tool_calls",
                        "calls": [
                            {
                                "id": "c1",
                                "name": "create_task",
                                "arguments": json.dumps(
                                    {"taskname": "卡片默认目录", "shareurl": "https://pan.quark.cn/s/nosave"}
                                ),
                            }
                        ],
                    }
                ],
                [{"type": "delta", "text": "请确认"}],
            ]
        ),
    )
    events = await _collect(ai_agent.run_turn(session_id, "帮我下载最新一集"))
    action = next(e for e in events if e["type"] == "done")["actions"][0]
    assert action["params"]["savepath"], "动作卡里的保存目录不能为空"
    assert "卡片默认目录" in action["params"]["savepath"]
