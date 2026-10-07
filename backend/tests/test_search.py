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


KKSO_HTML = """
<html><body><div class="list">
  <div class="item">
    <a href="javascript:;" onclick="linkBtn(this)" data-index="0" class="title">
      资源标题：凡人修仙传剧版 4K更新至24集资源描述：电影版紧接原著和动画年番中韩立的修仙之旅。
    </a>
    <!-- <div class="type cate">分类：其它</div> -->
    <div class="type time">2025-10-04</div>
    <div class="type"><span>来源：夸克网盘</span></div>
    <div class="btns">
      <div class="btn" @click.stop="copyText($event,'资源标题：凡人修仙传剧版 4K更新至24集资源描述：电影版紧接原著和动画年番中韩立的修仙之旅。','https://pan.quark.cn/s/4a1fe2f8929d','')"><i class="iconfont icon-fenxiang1"></i>复制分享</div>
      <a href="/d/13926.html" class="btn"><i class="iconfont icon-fangwen"></i>查看详情</a>
    </div>
  </div>
  <div class="item">
    <a href="javascript:;" onclick="linkBtn(this)" data-index="1" class="title">
      资源标题：三体&amp;凡人外传资源描述：科幻合集
    </a>
    <div class="type time">2026-03-01</div>
    <div class="type"><span>来源：夸克网盘</span></div>
    <div class="btns">
      <div class="btn" @click.stop="copyText($event,'资源标题：三体&amp;凡人外传资源描述：科幻合集','https://pan.quark.cn/s/b7c1d2e3f4a5','abcd')"><i class="iconfont icon-fenxiang1"></i>复制分享</div>
    </div>
  </div>
  <div class="item">
    <a href="javascript:;" onclick="linkBtn(this)" data-index="2" class="title">资源标题：只有百度的那条</a>
    <div class="type time">2026-02-02</div>
    <div class="type"><span>来源：百度网盘</span></div>
    <div class="btns">
      <div class="btn" @click.stop="copyText($event,'资源标题：只有百度的那条','https://pan.baidu.com/s/1abcdef','-')"><i class="iconfont icon-fenxiang1"></i>复制分享</div>
    </div>
  </div>
</div></body></html>
"""

KKSO_NO_ITEMS = "<html><body><div class=\"list\"><p>没有找到相关资源</p></div></body></html>"


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


KKSO_CFG = {"engines": [{"id": "k1", "type": "kkso", "name": "夸克搜", "server": "https://kkso.test"}]}


def kkso_client(body: str, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(200, text=body)

    return make_client(handler)


@pytest.mark.asyncio
async def test_kkso_search_requests_keyword_page_path():
    """kkso 的搜索入口是服务端渲染的 /s/<关键词>.html，没有 JSON 接口。"""
    from urllib.parse import unquote

    seen: list = []
    await search_all("凡人", False, KKSO_CFG, client=kkso_client(KKSO_HTML, seen))
    assert unquote(str(seen[0].url.path)) == "/s/凡人.html"


@pytest.mark.asyncio
async def test_kkso_search_keeps_only_quark_rows():
    out = await search_all("凡人", False, KKSO_CFG, client=kkso_client(KKSO_HTML))
    assert {r["shareurl"].split("?")[0] for r in out["data"]} == {
        "https://pan.quark.cn/s/4a1fe2f8929d",
        "https://pan.quark.cn/s/b7c1d2e3f4a5",
    }  # 百度那条不留：本项目只走夸克转存


@pytest.mark.asyncio
async def test_kkso_search_splits_title_and_description_and_reads_date():
    out = await search_all("凡人", False, KKSO_CFG, client=kkso_client(KKSO_HTML))
    first, second = out["data"]  # 按日期新的在前
    assert first["datetime"] == "2026-03-01"
    assert first["taskname"] == "三体&凡人外传"  # HTML 实体要还原
    assert first["content"] == "科幻合集"
    assert second["taskname"] == "凡人修仙传剧版 4K更新至24集"
    assert second["content"] == "电影版紧接原著和动画年番中韩立的修仙之旅。"
    assert second["channel"] == "夸克网盘"
    assert second["source"] == "夸克搜"


@pytest.mark.asyncio
async def test_kkso_search_appends_password_to_share_url():
    out = await search_all("凡人", False, KKSO_CFG, client=kkso_client(KKSO_HTML))
    assert out["data"][0]["shareurl"] == "https://pan.quark.cn/s/b7c1d2e3f4a5?pwd=abcd"
    assert out["data"][1]["shareurl"] == "https://pan.quark.cn/s/4a1fe2f8929d"  # 无提取码不拼


@pytest.mark.asyncio
async def test_kkso_search_says_structure_changed_instead_of_returning_empty():
    """站点改版抓不到条目时不能说「没搜到」，得让用户知道是这个源坏了。"""
    out = await search_all("凡人", False, KKSO_CFG, client=kkso_client(KKSO_NO_ITEMS))
    assert out["data"] == []
    assert out["errors"][0]["engine"] == "夸克搜"
    assert "结构" in out["errors"][0]["reason"]


KKSO_TWO_TITLE_FORMATS = """
<html><body><div class="list"><div class="item">
  <a href="javascript:;" class="title">【标题】：近20年贺岁片合集【描述】：英雄、手机、功夫、流浪地球、满江红下载地址</a>
  <div class="type time">2025-10-04</div>
  <div class="type"><span>来源：夸克网盘</span></div>
  <div class="btns"><div class="btn" @click.stop="copyText($event,'【标题】：近20年贺岁片合集【描述】：英雄、手机、功夫、流浪地球、满江红下载地址','https://pan.quark.cn/s/4bd3d7a0c047','')"><i></i>复制分享</div></div>
</div></div></body></html>
"""


@pytest.mark.asyncio
async def test_kkso_search_handles_the_bracket_title_format_too():
    """真机遇到两种标题写法：只认「资源标题：」会把一大段描述当剧名（建任务时名字就废了）。"""
    out = await search_all("流浪地球", False, KKSO_CFG, client=kkso_client(KKSO_TWO_TITLE_FORMATS))
    row = out["data"][0]
    assert row["taskname"] == "近20年贺岁片合集"
    assert row["content"].startswith("英雄、手机")


KKSO_BAIDU_ONLY = """
<html><body><div class="list"><div class="item">
  <a href="javascript:;" class="title">资源标题：只有百度的那条</a>
  <div class="type time">2026-02-02</div>
  <div class="type"><span>来源：百度网盘</span></div>
  <div class="btns"><div class="btn" @click.stop="copyText($event,'资源标题：只有百度的那条','https://pan.baidu.com/s/1abcdef','')"><i></i>复制分享</div></div>
</div></div></body></html>
"""


@pytest.mark.asyncio
async def test_kkso_search_names_no_quark_result_instead_of_blaming_structure():
    """关键词有结果但全是别家网盘：要说「没有夸克结果」，不能诬陷站点改版。"""
    out = await search_all("流浪地球", False, KKSO_CFG, client=kkso_client(KKSO_BAIDU_ONLY))
    assert out["data"] == []
    assert out["errors"][0]["reason"] == "夸克搜有结果但都不是夸克网盘，本项目转存不了"


@pytest.mark.asyncio
async def test_kkso_search_sends_browser_user_agent():
    """kkso 会挡默认 httpx UA：不带浏览器 UA 时页面里根本抓不到条目（真机验证踩到的）。"""
    seen: list = []
    await search_all("凡人", False, KKSO_CFG, client=kkso_client(KKSO_HTML, seen))
    assert "Mozilla" in seen[0].headers["user-agent"]


KKSO_SITE_SAYS_EMPTY = """
<html><body><div class="list"><div class="item">
  <span class="t">{{dialogItem.title}}</span>
</div><p>网盘接口暂时无响应</p></div></body></html>
"""


@pytest.mark.asyncio
async def test_kkso_search_uses_site_own_marker_for_no_results():
    """站点自己说「接口暂时无响应」时就是没结果，别报成页面结构变了。"""
    out = await search_all("不可能存在的剧名", False, KKSO_CFG, client=kkso_client(KKSO_SITE_SAYS_EMPTY))
    assert out["data"] == []
    assert out["errors"][0]["reason"] == "夸克搜没有这个关键词的结果"
