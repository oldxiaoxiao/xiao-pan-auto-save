"""AI 模型接入层：OpenAI Chat Completions 兼容协议 + Anthropic Messages 协议。

按 agent-design.md AG-01：不引入厂商 SDK，统一走 httpx；新增一家 = 新增一个协议分支。
工具调用（function calling / tool use）是分级能力，不支持时由上层降级为只读问答。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx

from ..config import PROXY


class AiError(RuntimeError):
    def __init__(self, message: str, kind: str = "upstream"):
        super().__init__(message)
        self.kind = kind  # config | auth | upstream | timeout | parse


@dataclass
class ProviderCfg:
    provider: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.2
    timeout_ms: int = 30_000

    def endpoint(self) -> str:
        base = (self.base_url or "").strip().rstrip("/")
        if not base:
            raise AiError("未配置服务地址（Base URL）", kind="config")
        return base + ("/messages" if self.provider == "anthropic" else "/chat/completions")


def _client(timeout_ms: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout_ms / 1000, proxy=PROXY or None)


# ---------------------------------------------------------------- OpenAI 协议


def _openai_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        if role == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""), "content": m.get("content", "")})
            continue
        item: dict = {"role": role, "content": m.get("content", "")}
        calls = m.get("tool_calls") or []
        if calls:
            item["tool_calls"] = [
                {"id": c.get("id", ""), "type": "function", "function": {"name": c["name"], "arguments": c.get("arguments", "{}")}}
                for c in calls
            ]
            if not item["content"]:
                item["content"] = None if role == "assistant" else ""
        out.append(item)
    return out


def _openai_tools(tools: list[dict]) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("parameters", {})}}
        for t in tools
    ]


async def _openai_stream(cfg: ProviderCfg, messages: list[dict], tools: list[dict]) -> AsyncIterator[dict]:
    body: dict = {
        "model": cfg.model,
        "messages": _openai_messages(messages),
        "temperature": cfg.temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = _openai_tools(tools)
        body["tool_choice"] = "auto"
    headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json"}
    async with _client(cfg.timeout_ms) as client:
        try:
            async with client.stream("POST", cfg.endpoint(), headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "ignore")[:300]
                    raise AiError(_http_hint(resp.status_code, detail), kind=_http_kind(resp.status_code))
                buf: dict[int, dict] = {}
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        yield {"type": "usage", **_norm_usage(chunk["usage"])}
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            yield {"type": "delta", "text": delta["content"]}
                        for tc in delta.get("tool_calls") or []:
                            idx = tc.get("index", 0)
                            cur = buf.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                            if tc.get("id"):
                                cur["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"):
                                cur["name"] = fn["name"]
                            if fn.get("arguments"):
                                cur["arguments"] += fn["arguments"]
                if buf:
                    yield {"type": "tool_calls", "calls": [buf[i] for i in sorted(buf)]}
        except httpx.TimeoutException as exc:
            raise AiError(f"模型服务超时（{cfg.timeout_ms}ms）：{exc}", kind="timeout") from exc
        except httpx.HTTPError as exc:
            raise AiError(f"无法连接模型服务：{exc}", kind="upstream") from exc


# ------------------------------------------------------------- Anthropic 协议


def _anth_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    system = "".join(m["content"] for m in messages if m.get("role") == "system")
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": m.get("tool_call_id", ""),
                            "content": m.get("content", ""),
                        }
                    ],
                }
            )
            continue
        if role == "assistant":
            blocks: list[dict] = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                try:
                    args = json.loads(c.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": c.get("id", ""), "name": c["name"], "input": args})
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            continue
        out.append({"role": "user", "content": m.get("content", "")})
    return system, out


def _anth_tools(tools: list[dict]) -> list[dict]:
    return [
        {"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("parameters", {})}
        for t in tools
    ]


async def _anthropic_stream(cfg: ProviderCfg, messages: list[dict], tools: list[dict]) -> AsyncIterator[dict]:
    system, msgs = _anth_messages(messages)
    body: dict = {
        "model": cfg.model,
        "messages": msgs,
        "system": system,
        "max_tokens": 4096,
        "temperature": cfg.temperature,
        "stream": True,
    }
    if tools:
        body["tools"] = _anth_tools(tools)
    headers = {
        "x-api-key": cfg.api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    async with _client(cfg.timeout_ms) as client:
        try:
            async with client.stream("POST", cfg.endpoint(), headers=headers, json=body) as resp:
                if resp.status_code >= 400:
                    detail = (await resp.aread()).decode("utf-8", "ignore")[:300]
                    raise AiError(_http_hint(resp.status_code, detail), kind=_http_kind(resp.status_code))
                blocks: dict[int, dict] = {}
                usage: dict = {}
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        event = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    etype = event.get("type")
                    if etype == "message_start":
                        usage.update(_norm_usage(event.get("message", {}).get("usage") or {}))
                    elif etype == "content_block_start":
                        block = event.get("content_block") or {}
                        blocks[event.get("index", 0)] = {
                            "type": block.get("type"),
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "json": "",
                        }
                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        cur = blocks.setdefault(event.get("index", 0), {"type": "text", "id": "", "name": "", "json": ""})
                        if delta.get("type") == "text_delta":
                            yield {"type": "delta", "text": delta.get("text", "")}
                        elif delta.get("type") == "input_json_delta":
                            cur["json"] += delta.get("partial_json", "")
                    elif etype == "message_delta":
                        usage.update(_norm_usage(event.get("usage") or {}))
                calls = [
                    {"id": b["id"], "name": b["name"], "arguments": b["json"] or "{}"}
                    for b in blocks.values()
                    if b["type"] == "tool_use" and b["name"]
                ]
                if usage:
                    yield {"type": "usage", **usage}
                if calls:
                    yield {"type": "tool_calls", "calls": calls}
        except httpx.TimeoutException as exc:
            raise AiError(f"模型服务超时（{cfg.timeout_ms}ms）：{exc}", kind="timeout") from exc
        except httpx.HTTPError as exc:
            raise AiError(f"无法连接模型服务：{exc}", kind="upstream") from exc


# ------------------------------------------------------------------- 公共入口


def _norm_usage(raw: dict) -> dict:
    return {
        "prompt_tokens": int(raw.get("prompt_tokens") or raw.get("input_tokens") or 0),
        "completion_tokens": int(raw.get("completion_tokens") or raw.get("output_tokens") or 0),
    }


def _http_kind(status: int) -> str:
    return "auth" if status in (401, 403) else "upstream"


def _http_hint(status: int, detail: str) -> str:
    if status in (401, 403):
        return f"API Key 无效或没有该模型的访问权限（HTTP {status}）"
    if status == 404:
        return f"模型或服务地址不存在（HTTP 404），请检查 Base URL 与模型名：{detail}"
    if status == 429:
        return f"模型服务限流或额度耗尽（HTTP 429）：{detail}"
    return f"模型服务返回 HTTP {status}：{detail}"


async def stream_chat(cfg: ProviderCfg, messages: list[dict], tools: list[dict]) -> AsyncIterator[dict]:
    """统一流式入口，逐段产出事件。

    事件：{"type": "delta"|"tool_calls"|"usage"|"error", ...}
    """
    if not cfg.model:
        raise AiError("未配置模型名", kind="config")
    if not cfg.api_key:
        raise AiError("未配置 API Key，请到「设置 → AI 助手」填写", kind="config")
    stream = _anthropic_stream(cfg, messages, tools) if cfg.provider == "anthropic" else _openai_stream(
        cfg, messages, tools
    )
    async for event in stream:
        yield event


async def test_connection(cfg: ProviderCfg) -> dict:
    """连通性测试：返回是否可用 + 是否支持工具调用（能力分级）。"""
    messages = [{"role": "user", "content": "回复 OK 两个字母"}]
    text = ""
    try:
        async for event in stream_chat(cfg, messages, []):
            if event["type"] == "delta":
                text += event["text"]
    except AiError as exc:
        return {"ok": False, "message": str(exc), "kind": exc.kind, "tool_call": False}
    return {
        "ok": True,
        "message": (text.strip()[:40] or "已连通"),
        "kind": "",
        # 工具调用是协议能力：协议支持即声明支持，实际可用性以运行期为准
        "tool_call": True,
    }
