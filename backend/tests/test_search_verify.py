"""搜索探活：失效链接必须在展示前被剔掉，网络抖动不能误杀。

这里一律用注入的假探针，不碰真实网络（conftest 已把默认探针换成"一律 unknown"）。
"""

from __future__ import annotations

import httpx
import pytest

from backend.services import search_service
from backend.services.search_service import search_all, verify_links

PANSOU_RESP = {
    "code": 0,
    "data": {
        "merged_by_type": {
            "quark": [
                {"url": "https://pan.quark.cn/s/dead1", "note": "已过期资源", "datetime": "2026-10-20T10:00:00Z"},
                {"url": "https://pan.quark.cn/s/alive1", "note": "存活资源", "datetime": "2026-10-19T10:00:00Z"},
                {"url": "https://pan.quark.cn/s/dead2", "note": "另一条过期", "datetime": "2026-10-18T10:00:00Z"},
            ]
        }
    },
}

CFG = {"engines": [{"id": "p1", "type": "pansou", "name": "公共站", "server": "https://ps.test", "enable": True}]}


def _client() -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=PANSOU_RESP)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _row(url: str) -> dict:
    return {"shareurl": url, "taskname": "x", "content": "", "datetime": "", "channel": "", "source": "公共站"}


@pytest.mark.asyncio
async def test_dead_links_are_dropped_and_counted():
    async def probe(url: str, timeout: float) -> str:
        return search_service.VERDICT_DEAD if "dead" in url else search_service.VERDICT_OK

    out = await search_all("凡人", False, CFG, client=_client(), probe=probe)
    assert [r["shareurl"] for r in out["data"]] == ["https://pan.quark.cn/s/alive1"]
    assert out["stats"]["checked"] == 3
    assert out["stats"]["dead"] == 2


@pytest.mark.asyncio
async def test_all_dead_says_links_expired_not_keyword_wrong():
    """全被过滤掉时必须说"结果集体过期"，否则用户以为是关键词写错了。"""

    async def probe(url: str, timeout: float) -> str:
        return search_service.VERDICT_DEAD

    out = await search_all("凡人", False, CFG, client=_client(), probe=probe)
    assert out["data"] == []
    reasons = " ".join(e["reason"] for e in out["errors"])
    assert "全部已失效" in reasons


@pytest.mark.asyncio
async def test_network_error_is_not_treated_as_dead():
    """探针超时/异常只能是 unknown：公共源本来就抖，误杀比放过更糟。"""

    async def probe(url: str, timeout: float) -> str:
        raise TimeoutError

    out = await search_all("凡人", False, CFG, client=_client(), probe=probe)
    assert len(out["data"]) == 3
    assert out["stats"]["dead"] == 0


@pytest.mark.asyncio
async def test_probe_budget_keeps_latency_bounded():
    """只探前 limit 条：搜到几十条时不能把等待拖成十几秒。"""
    calls: list[str] = []

    async def probe(url: str, timeout: float) -> str:
        calls.append(url)
        return search_service.VERDICT_OK

    rows = [_row(f"https://pan.quark.cn/s/a{i}") for i in range(10)]
    kept, stats = await verify_links(rows, limit=4, probe=probe)
    assert len(calls) == 4
    assert stats["checked"] == 4
    assert stats["skipped"] == 6
    assert len(kept) == 10  # 没探的保留，不因为"没查"就丢


@pytest.mark.asyncio
async def test_non_quark_links_are_not_probed():
    """别的网盘探了也没用（本项目只转存夸克），直接按 unknown 保留，别浪费请求。"""
    calls: list[str] = []

    async def probe(url: str, timeout: float) -> str:
        calls.append(url)
        return search_service.VERDICT_OK

    rows = [_row("https://pan.baidu.com/s/1abc"), _row("https://pan.quark.cn/s/a1")]
    kept, stats = await verify_links(rows, probe=probe)
    assert calls == ["https://pan.quark.cn/s/a1"]
    assert len(kept) == 2
    assert stats["unknown"] == 1


@pytest.mark.asyncio
async def test_verdict_is_cached_across_searches(monkeypatch):
    """同一个关键词反复搜（输入框防抖后常有）不该重复探活。"""
    monkeypatch.setattr(search_service, "_VERIFY_TTL", 600)
    search_service.reset_verify_cache()
    calls: list[str] = []

    async def probe(url: str, timeout: float) -> str:
        calls.append(url)
        return search_service.VERDICT_OK

    rows = [_row("https://pan.quark.cn/s/cache1")]
    await verify_links(rows, probe=probe)
    await verify_links(rows, probe=probe)
    assert len(calls) == 1

    # 死链也进缓存：不然每次搜索都要为同一批死链再打一遍夸克接口
    dead_calls: list[str] = []

    async def dead_probe(url: str, timeout: float) -> str:
        dead_calls.append(url)
        return search_service.VERDICT_DEAD

    dead_rows = [_row("https://pan.quark.cn/s/cache2")]
    await verify_links(dead_rows, probe=dead_probe)
    await verify_links(dead_rows, probe=dead_probe)
    assert len(dead_calls) == 1


@pytest.mark.asyncio
async def test_verify_can_be_disabled():
    """AI 工具那条链路要的是"先看一眼有什么"，允许关掉探活。"""

    async def probe(url: str, timeout: float) -> str:
        raise AssertionError("关掉探活后不应该被调用")

    out = await search_all("凡人", False, CFG, client=_client(), verify=False, probe=probe)
    assert len(out["data"]) == 3
    assert out["stats"] == {}
