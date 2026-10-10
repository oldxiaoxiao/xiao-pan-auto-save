"""对话式 AI 助手：配置、会话、流式对话、动作卡执行与用量。

鉴权沿用管理会话（WebAuthMiddleware），不向外部 Token API 开放——避免脚本滥用用户自己的 Key。
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select
from starlette.responses import StreamingResponse

from ..database import session_scope
from ..models import AI_MODES, AI_PROVIDERS, AiChatMessage, AiChatSession, AiProvider, AiUsage
from ..services.ai_agent import AgentError, load_provider_cfg, run_action, run_turn
from ..services.ai_client import ProviderCfg, test_connection
from ..services.ai_secret import encrypt, mask

router = APIRouter(prefix="/api/agent", tags=["agent"])


class ConfigIn(BaseModel):
    provider: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = ""  # 留空表示保持原值
    clear_key: bool = False
    temperature: float = 0.2
    timeout_ms: int = 30_000
    mode: str = "copilot"
    monthly_token_limit: int = 0
    enabled: bool = True
    tool_call: bool = True


class SessionIn(BaseModel):
    title: str = ""


class ChatIn(BaseModel):
    content: str = Field(min_length=1)


class ActionIn(BaseModel):
    params: dict = Field(default_factory=dict)


class FeedbackIn(BaseModel):
    feedback: str = ""


def _get_or_create() -> AiProvider:
    with session_scope() as session:
        row = session.exec(select(AiProvider).where(AiProvider.key == "default")).first()
        if row is None:
            row = AiProvider(key="default")
            session.add(row)
            session.commit()
            session.refresh(row)
        return row


def _masked_row(row: AiProvider) -> dict:
    api_key = ""
    if row.api_key_enc:
        try:
            from ..services.ai_secret import decrypt

            api_key = mask(decrypt(row.api_key_enc))
        except Exception:  # noqa: BLE001 密钥文件丢失时不能让配置页打不开
            api_key = ""
    return {
        "enabled": row.enabled,
        "provider": row.provider,
        "base_url": row.base_url,
        "model": row.model,
        "has_key": bool(row.api_key_enc),
        "api_key_masked": api_key,
        "temperature": row.temperature,
        "timeout_ms": row.timeout_ms,
        "mode": row.mode,
        "monthly_token_limit": row.monthly_token_limit,
        "tool_call": row.tool_call,
    }


@router.get("/config")
async def get_config() -> dict:
    return {"ok": True, "data": _masked_row(_get_or_create())}


@router.put("/config")
async def save_config(body: ConfigIn) -> dict:
    if body.provider not in AI_PROVIDERS:
        raise HTTPException(400, f"provider 只能是 {' / '.join(AI_PROVIDERS)}")
    if body.mode not in AI_MODES:
        raise HTTPException(400, f"mode 只能是 {' / '.join(AI_MODES)}")
    if not 0 <= body.temperature <= 2:
        raise HTTPException(400, "temperature 需在 0~2 之间")
    with session_scope() as session:
        row = session.exec(select(AiProvider).where(AiProvider.key == "default")).first()
        if row is None:
            row = AiProvider(key="default")
        row.provider = body.provider
        row.base_url = body.base_url.strip()
        row.model = body.model.strip()
        row.temperature = body.temperature
        row.timeout_ms = max(1000, min(body.timeout_ms, 300_000))
        row.mode = body.mode
        row.monthly_token_limit = max(0, body.monthly_token_limit)
        row.enabled = body.enabled
        row.tool_call = body.tool_call
        if body.clear_key:
            row.api_key_enc = ""
        elif body.api_key.strip():
            row.api_key_enc = encrypt(body.api_key.strip())
        row.updated_at = datetime.now()
        session.add(row)
        session.commit()
        session.refresh(row)
        return {"ok": True, "data": _masked_row(row)}


@router.post("/test")
async def test(body: ConfigIn) -> dict:
    """连通性测试：优先测传入值，没传关键项时测已保存的配置。"""
    if body.base_url and body.model and (body.api_key.strip() or body.clear_key is False):
        key = body.api_key.strip()
        if not key:
            row = _get_or_create()
            try:
                from ..services.ai_secret import decrypt

                key = decrypt(row.api_key_enc) if row.api_key_enc else ""
            except Exception:  # noqa: BLE001
                key = ""
        cfg = ProviderCfg(
            provider=body.provider,
            base_url=body.base_url.strip(),
            model=body.model.strip(),
            api_key=key,
            temperature=body.temperature,
            timeout_ms=max(1000, min(body.timeout_ms, 300_000)),
        )
    else:
        try:
            cfg, _ = load_provider_cfg()
        except AgentError as exc:
            return {"ok": False, "message": str(exc), "kind": exc.kind, "tool_call": False}
    return await test_connection(cfg)


@router.get("/sessions")
async def list_sessions() -> dict:
    with session_scope() as session:
        rows = session.exec(select(AiChatSession).order_by(AiChatSession.last_at.desc()).limit(50)).all()
    return {
        "ok": True,
        "data": [
            {"id": r.id, "title": r.title, "mode": r.mode, "last_at": r.last_at.isoformat(timespec="seconds")}
            for r in rows
        ],
    }


@router.post("/sessions")
async def create_session(body: SessionIn) -> dict:
    with session_scope() as session:
        row = AiChatSession(title=body.title or "新对话")
        session.add(row)
        session.commit()
        session.refresh(row)
        return {"ok": True, "data": {"id": row.id, "title": row.title}}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int) -> dict:
    with session_scope() as session:
        row = session.get(AiChatSession, session_id)
        if row is None:
            raise HTTPException(404, "会话不存在")
        session.delete(row)
        for m in session.exec(select(AiChatMessage).where(AiChatMessage.session_id == session_id)).all():
            session.delete(m)
    return {"ok": True}


def _message_out(m: AiChatMessage) -> dict:
    try:
        sources = json.loads(m.sources or "[]")
    except json.JSONDecodeError:
        sources = []
    try:
        actions = json.loads(m.actions or "[]")
    except json.JSONDecodeError:
        actions = []
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "sources": sources,
        "actions": actions,
        "confidence": m.confidence,
        "model": m.model,
        "prompt_version": m.prompt_version,
        "tools_version": m.tools_version,
        "feedback": m.feedback,
        "error": m.error,
        "created_at": m.created_at.isoformat(timespec="seconds"),
    }


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: int) -> dict:
    with session_scope() as session:
        if session.get(AiChatSession, session_id) is None:
            raise HTTPException(404, "会话不存在")
        rows = session.exec(
            select(AiChatMessage).where(AiChatMessage.session_id == session_id).order_by(AiChatMessage.id)
        ).all()
    return {"ok": True, "data": [_message_out(m) for m in rows]}


@router.post("/sessions/{session_id}/messages")
async def chat(session_id: int, body: ChatIn) -> StreamingResponse:
    with session_scope() as session:
        if session.get(AiChatSession, session_id) is None:
            raise HTTPException(404, "会话不存在")

    async def event_stream():
        async for event in run_turn(session_id, body.content):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/actions/{action_id}/execute")
async def execute_action(action_id: str, body: ActionIn) -> dict:
    return await run_action(action_id, body.params or None)


@router.post("/messages/{message_id}/feedback")
async def feedback(message_id: int, body: FeedbackIn) -> dict:
    allowed = ("", "up", "down", "action_confirmed", "action_edited", "action_cancelled")
    if body.feedback not in allowed:
        raise HTTPException(400, "反馈值不合法")
    with session_scope() as session:
        row = session.get(AiChatMessage, message_id)
        if row is None:
            raise HTTPException(404, "消息不存在")
        row.feedback = body.feedback
        session.add(row)
    return {"ok": True}


@router.get("/usage")
async def usage() -> dict:
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as session:
        rows = session.exec(select(AiUsage).where(AiUsage.created_at >= month_start)).all()
    by_model: dict[str, dict] = {}
    for r in rows:
        item = by_model.setdefault(
            r.model or "-", {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0}
        )
        item["calls"] += 1
        item["failed"] += 0 if r.ok else 1
        item["prompt_tokens"] += r.prompt_tokens
        item["completion_tokens"] += r.completion_tokens
    return {
        "ok": True,
        "data": {
            "since": month_start.isoformat(timespec="seconds"),
            "calls": len(rows),
            "failed": sum(1 for r in rows if not r.ok),
            "prompt_tokens": sum(r.prompt_tokens for r in rows),
            "completion_tokens": sum(r.completion_tokens for r in rows),
            "by_model": by_model,
        },
    }
