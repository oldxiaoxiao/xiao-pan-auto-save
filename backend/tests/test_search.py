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


def pansou_body(rows):
    return {"code": 0, "data": {"merged_by_type": {"quark": rows}}}


PS_MULTI = {
    "https://ps1.test": pansou_body(
        [
            {"url": "https://pan.quark.cn/s/aaa", "note": "凡人【简介】第1集", "datetime": "2026-10-01T10:00:00+08:00"},
            {"url": "https://pan.quark.cn/s/bbb?pwd=ab12", "note": "凡人【简介】第2集", "datetime": "2026-10-02T10:00:00+08:00"},
            {"url": "https://pan.quark.cn/s/ccc", "note": "凡人【简介】无时间", "datetime": ""},
        ]
    ),
    # 备用站：同一条链接带尾斜杠（不同写法）、时间更新；外加一条只有它有的
    "https://ps2.test": pansou_body(
        [
            {"url": "https://pan.quark.cn/s/aaa/", "note": "凡人 修仙传 全集", "datetime": "2026-10-05T10:00:00+08:00"},
            {"url": "https://pan.quark.cn/s/bbb?pwd=zz99", "note": "凡人【简介】第2集另提取码", "datetime": "2026-10-02T09:00:00+08:00"},
            {"url": "https://pan.quark.cn/s/ddd", "note": "凡人【简介】仅备用站有", "datetime": "2026-09-01T10:00:00+08:00"},
        ]
    ),
}

TWO_PANSOU = {
    "engines": [
        {"id": "e1", "type": "pansou", "name": "公共站", "server": "https://ps1.test"},
        {"id": "e2", "type": "pansou", "name": "备用站", "server": "https://ps2.test"},
    ]
}


def ps_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=PS_MULTI[f"https://{request.url.host}"])


@pytest.mark.asyncio
async def test_same_link_from_two_engines_merges_into_one_row():
    out = await search_all("凡人", False, TWO_PANSOU, client=make_client(ps_handler))
    aaa = [r for r in out["data"] if r["shareurl"].rstrip("/").endswith("/aaa")]
    assert len(aaa) == 1  # 尾斜杠写法不同也算同一条
    merged = aaa[0]
    assert merged["source"] == "公共站 + 备用站"  # 谁给的不丢
    assert merged["datetime"] == "2026-10-05 10:00"  # 取较新时间
    assert merged["taskname"] == "凡人"  # 保留先出现（配置顺序）的清洗结果


@pytest.mark.asyncio
async def test_different_pwd_query_means_different_share():
    """提取码不同的两条链接不能合并，否则转存会拿到失效/错误的一条。"""
    out = await search_all("凡人", False, TWO_PANSOU, client=make_client(ps_handler))
    bbb = [r for r in out["data"] if "/bbb" in r["shareurl"]]
    assert {r["shareurl"] for r in bbb} == {
        "https://pan.quark.cn/s/bbb?pwd=ab12",
        "https://pan.quark.cn/s/bbb?pwd=zz99",
    }


@pytest.mark.asyncio
async def test_no_datetime_sorts_last_and_newest_first():
    out = await search_all("凡人", False, TWO_PANSOU, client=make_client(ps_handler))
    times = [r["datetime"] for r in out["data"]]
    assert times == ["2026-10-05 10:00", "2026-10-02 10:00", "2026-10-02 09:00", "2026-09-01 10:00", ""]


@pytest.mark.asyncio
async def test_selected_engine_hits_only_that_server():
    hosts = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        return httpx.Response(200, json=PS_MULTI[f"https://{request.url.host}"])

    out = await search_all("凡人", False, TWO_PANSOU, "e2", client=make_client(handler))
    assert hosts == ["ps2.test"]
    assert {r["source"] for r in out["data"]} == {"备用站"}


@pytest.mark.asyncio
async def test_stale_engine_selection_reports_instead_of_searching_all():
    out = await search_all("凡人", False, TWO_PANSOU, "gone", client=make_client(ps_handler))
    assert out["data"] == []
    assert "不存在" in out["errors"][0]["reason"]


@pytest.mark.asyncio
async def test_one_engine_failing_reports_which_and_keeps_the_other():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ps2.test":
            raise httpx.ConnectError("备用站挂了")
        return httpx.Response(200, json=PS_MULTI["https://ps1.test"])

    out = await search_all("凡人", False, TWO_PANSOU, client=make_client(handler))
    assert [r["shareurl"] for r in out["data"]] == [
        "https://pan.quark.cn/s/bbb?pwd=ab12",
        "https://pan.quark.cn/s/aaa",
        "https://pan.quark.cn/s/ccc",
    ]
    assert out["errors"] == [{"engine": "备用站", "reason": "备用站挂了"}]


@pytest.mark.asyncio
async def test_business_failure_reason_from_engine_is_shown():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ps1.test":
            return httpx.Response(200, json={"code": 500, "message": "触发限流"})
        return httpx.Response(200, json=PS_MULTI["https://ps2.test"])

    out = await search_all("凡人", False, TWO_PANSOU, client=make_client(handler))
    assert out["errors"] == [{"engine": "公共站", "reason": "触发限流"}]
    assert all(r["source"] == "备用站" for r in out["data"])


@pytest.mark.asyncio
async def test_non_json_response_is_explained_in_human_terms():
    """公共站被限流时返回的是 HTML 错误页：不能把 JSONDecodeError 的原文甩给用户。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ps1.test":
            return httpx.Response(429, text="<html>Too Many Requests</html>")
        return httpx.Response(200, json=PS_MULTI["https://ps2.test"])

    out = await search_all("凡人", False, TWO_PANSOU, client=make_client(handler))
    assert out["errors"] == [{"engine": "公共站", "reason": "返回的不是 JSON（HTTP 429），可能被限流"}]
    assert all(r["source"] == "备用站" for r in out["data"])


@pytest.mark.asyncio
async def test_timeout_reason_says_so():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("读超时", request=request)

    out = await search_all("凡人", False, TWO_PANSOU, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert {e["engine"] for e in out["errors"]} == {"公共站", "备用站"}
    assert all(e["reason"] == "请求超时" for e in out["errors"])


@pytest.mark.asyncio
async def test_engines_are_queried_concurrently():
    """每个源 15s 超时：串行会逐个叠加，并发只等最慢的一个。"""
    import asyncio
    import time

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=PS_MULTI[f"https://{request.url.host}"])

    started = time.perf_counter()
    await search_all("凡人", False, TWO_PANSOU, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert time.perf_counter() - started < 0.35


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
async def test_cloudsaver_token_refresh_is_written_back_to_that_engine():
    """多实例时新 token 只能写回它自己的引擎，不能串到别的 CloudSaver 上。"""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url.path), request.headers.get("authorization")))
        if request.url.host == "cs2.test":
            return httpx.Response(200, json=CS_SEARCH_OK)
        if request.method == "GET" and request.headers["authorization"] == "Bearer OLD":
            return httpx.Response(200, json={"success": False, "message": "无效的 token"})
        if request.url.path == "/api/user/login":
            return httpx.Response(200, json={"success": True, "data": {"token": "NEW"}})
        return httpx.Response(200, json=CS_SEARCH_OK)

    cfg = {
        "engines": [
            {
                "id": "cs1",
                "type": "cloudsaver",
                "name": "家里",
                "server": "https://cs.test",
                "username": "u",
                "password": "p",
                "token": "OLD",
            },
            {
                "id": "cs2",
                "type": "cloudsaver",
                "name": "公司",
                "server": "https://cs2.test",
                "username": "u2",
                "password": "p2",
                "token": "GOOD",
            },
        ]
    }
    out = await search_all("流浪", False, cfg, client=make_client(handler))
    assert out["token_updates"] == {"cs1": "NEW"}
    rows = out["data"]
    assert rows[0]["shareurl"] == "https://pan.quark.cn/s/cs1"
    assert rows[0]["taskname"] == "流浪地球 链接"
    assert rows[0]["content"] == "科幻大片"
    assert rows[0]["source"] == "家里 + 公司"
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
