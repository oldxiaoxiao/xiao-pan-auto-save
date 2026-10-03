"""搜索聚合服务测试：httpx.MockTransport，零真实网络。"""

from __future__ import annotations

import httpx
import pytest

from backend.services.search_service import search_all

PANSOUResp = {
    "code": 0,
    "data": {
        "merged_by_type": {
            "quark": [
                {
                    "url": "https://pan.quark.cn/s/aaa",
                    "note": "凡人修仙传【简介】最新42集",
                    "datetime": "2026-10-01T10:00:00+08:00",
                    "source": "tgchan",
                },
                {
                    "url": "https://pan.quark.cn/s/bbb",
                    "note": "三体 简介：经典科幻",
                    "datetime": "2026-09-30T08:00:00Z",
                    "source": "x",
                },
                {"url": "https://pan.quark.cn/s/aaa", "note": "重复链接", "datetime": "", "source": ""},
            ]
        }
    },
}

CS_SEARCH_OK = {
    "success": True,
    "data": [
        {
            "list": [
                {
                    "title": "名称：流浪地球 链接",
                    "content": "描述：科幻大片标签",
                    "pubDate": "2026-10-01T12:00:00+08:00",
                    "channelId": "ch1",
                    "cloudLinks": [
                        {"cloudType": "quark", "link": "https://pan.quark.cn/s/cs1"},
                        {"cloudType": "baidu", "link": "https://pan.baidu.com/x"},
                    ],
                }
            ]
        }
    ],
}


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_pansou_clean_dedup_and_cst():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["kw"] == "凡人"
        return httpx.Response(200, json=PANSOUResp)

    out = await search_all(
        "凡人", False, {"pansou": {"server": "https://ps.test"}}, client=make_client(handler)
    )
    rows = out["data"]
    assert len(rows) == 2  # 重复链接被去重
    assert rows[0]["taskname"] == "凡人修仙传"
    assert rows[0]["content"] == "最新42集"
    assert rows[1]["datetime"] == "2026-09-30 16:00"  # UTC → CST
    assert rows[0]["source"] == "PanSou"


@pytest.mark.asyncio
async def test_cloudsaver_token_refresh_cycle():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url.path), request.headers.get("authorization")))
        if request.method == "GET" and request.headers["authorization"] == "Bearer OLD":
            return httpx.Response(200, json={"success": False, "message": "无效的 token"})
        if request.url.path == "/api/user/login":
            return httpx.Response(200, json={"success": True, "data": {"token": "NEW"}})
        return httpx.Response(200, json=CS_SEARCH_OK)

    cfg = {"cloudsaver": {"server": "https://cs.test", "username": "u", "password": "p", "token": "OLD"}}
    out = await search_all("流浪", False, cfg, client=make_client(handler))
    assert out["new_cs_token"] == "NEW"
    rows = out["data"]
    assert rows[0]["shareurl"] == "https://pan.quark.cn/s/cs1"
    assert rows[0]["taskname"] == "流浪地球 链接"
    assert rows[0]["content"] == "科幻大片"
    assert ("POST", "/api/user/login") in [(m, p) for m, p, _ in calls]


@pytest.mark.asyncio
async def test_unreachable_source_degrades_silently():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    out = await search_all(
        "x",
        False,
        {
            "pansou": {"server": "https://ps.test"},
            "cloudsaver": {"server": "https://cs.test", "username": "u", "password": "p"},
        },
        client=make_client(handler),
    )
    assert out["data"] == []


@pytest.mark.asyncio
async def test_disabled_source_skipped():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("不应被调用")

    cfg = {"pansou": {"enable": "false", "server": "https://ps.test"}}
    out = await search_all("x", False, cfg, client=make_client(handler))
    assert out["data"] == []
