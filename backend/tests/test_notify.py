"""notify_service 测试：用 httpx.MockTransport 拦截全部请求，不打真实网络。"""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import httpx
import pytest

from backend.services import notify_service as ns


class FakeHttp:
    """MockTransport 路由器：按 URL 子串返回预设响应或抛出异常。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: list[tuple[str, object]] = []

    def route(self, needle: str, response: httpx.Response | Exception | float) -> None:
        """needle 命中请求 URL 子串时返回 response；float 表示模拟挂起秒数（测超时）。"""
        self.routes.append((needle, response))

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        for needle, response in self.routes:
            if needle in url or needle == request.method:
                if isinstance(response, Exception):
                    raise response
                if isinstance(response, float):
                    await asyncio.sleep(response)
                    return httpx.Response(200, json={})
                assert isinstance(response, httpx.Response)
                return response
        return httpx.Response(200, json=self.default_success())

    @staticmethod
    def default_success() -> dict:
        """尽可能满足各渠道成功判定字段的通用响应。"""
        return {
            "code": 200,
            "StatusCode": 0,
            "errcode": 0,
            "errno": 0,
            "ret": 0,
            "status": "ok",
            "status_code": 0,
            "ok": True,
            "id": 1,
            "message": "success",
            "errmsg": "ok",
            "data": "flow-id",
            "content": {"result": ["pushed"]},
        }

    def request_body(self, index: int = -1) -> bytes:
        return self.requests[index].content

    def find_request(self, needle: str) -> httpx.Request:
        for req in self.requests:
            if needle in str(req.url):
                return req
        raise AssertionError(f"未捕获到请求: {needle}，已有: {[str(r.url) for r in self.requests]}")


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeHttp:
    fake_http = FakeHttp()
    monkeypatch.setattr(ns, "_transport", httpx.MockTransport(fake_http.handler))
    return fake_http


# ---------------------------------------------------------------------------
# push_all：并发多渠道成功 / 失败隔离
# ---------------------------------------------------------------------------


async def test_push_all_multi_channel_success_and_failure_isolation(fake: FakeHttp):
    fake.route("api.day.app", httpx.Response(200, json={"code": 200}))
    fake.route("open.feishu.cn", httpx.Response(200, json={"StatusCode": 0}))
    fake.route("qyapi.weixin.qq.com", httpx.Response(200, json={"errcode": 40096, "errmsg": "invalid key"}))
    fake.route("api.telegram.org", httpx.ConnectError("connection refused"))

    cfg = {
        "BARK_PUSH": "https://api.day.app/devkey/",
        "FSKEY": "feishu-hook-key",
        "QYWX_KEY": "bad-wecom-key",
        "TG_BOT_TOKEN": "123:abc",
        "TG_USER_ID": "456",
        "IGOT_PUSH_KEY": "未配置成功响应，走默认成功",
    }
    logs: list[tuple[str, str]] = []
    results = await ns.push_all("标题", "内容", cfg, log=lambda lv, m: logs.append((lv, m)))

    by_channel = {name: (ok, msg) for name, ok, msg in results}
    assert set(by_channel) == {"BARK", "FEISHU", "QYWX_KEY", "TG_BOT", "IGOT"}
    assert by_channel["BARK"][0] is True
    assert by_channel["FEISHU"][0] is True
    assert by_channel["IGOT"][0] is True
    # 单渠道协议失败 / 网络失败均不影响其他渠道
    assert by_channel["QYWX_KEY"][0] is False
    assert "invalid key" in by_channel["QYWX_KEY"][1]
    assert by_channel["TG_BOT"][0] is False
    assert "网络错误" in by_channel["TG_BOT"][1]
    assert logs and all(lv in ("INFO", "WARNING", "ERROR") for lv, _ in logs)


async def test_push_all_timeout_isolation(fake: FakeHttp, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ns, "CHANNEL_TIMEOUT", 0.2)
    fake.route("api.day.app", 3.0)  # bark 挂起模拟超时

    cfg = {"BARK_PUSH": "https://api.day.app/devkey/", "FSKEY": "ok-key"}
    results = await ns.push_all("标题", "内容", cfg)
    by_channel = {name: (ok, msg) for name, ok, msg in results}
    assert by_channel["BARK"][0] is False
    assert "超时" in by_channel["BARK"][1]
    assert by_channel["FEISHU"][0] is True


async def test_push_all_empty_and_skip(fake: FakeHttp):
    cfg = {"FSKEY": "k", "SKIP_PUSH_TITLE": "任务通知\n另一个标题"}
    assert await ns.push_all("标题", "", cfg) == []  # 内容为空
    assert await ns.push_all("任务通知", "内容", cfg) == []  # 命中 SKIP_PUSH_TITLE
    assert fake.requests == []
    results = await ns.push_all("其他标题", "内容", cfg)
    assert [r[0] for r in results] == ["FEISHU"]


# ---------------------------------------------------------------------------
# enabled_channels 判定
# ---------------------------------------------------------------------------


def test_enabled_channels_basic():
    cfg = {
        "BARK_PUSH": "devkey",
        "CONSOLE": True,
        "DD_BOT_TOKEN": "token",  # 缺 DD_BOT_SECRET，不启用
        "FSKEY": "feishu",
        "QMSG_KEY": "qk",  # 缺 QMSG_TYPE，不启用
        "PUSH_KEY": "SCTxxxx",
        "QYWX_AM": "corpid,secret,@all,1000002",
        "TG_BOT_TOKEN": "1:abc",
        "TG_USER_ID": "42",
        "WEBHOOK_URL": "https://h/$title",
        "WEBHOOK_METHOD": "post",
    }
    names = ns.enabled_channels(cfg)
    assert "BARK" in names and "CONSOLE" in names and "FEISHU" in names
    assert "SERVERCHAN" in names and "QYWX_AM" in names and "TG_BOT" in names
    assert "WEBHOOK" in names
    assert "DD_BOT" not in names and "QMSG" not in names
    # 未配置渠道 None/空/False 一律跳过
    assert ns.enabled_channels({k: v for k, v in cfg.items() if k != "FSKEY"}).count("FEISHU") == 0
    assert ns.enabled_channels({"BARK_PUSH": None, "FSKEY": "", "IGOT_PUSH_KEY": False}) == []


def test_enabled_channels_qywx_am_media_id_variant():
    plain = {"QYWX_AM": "corp,secret,@all,10"}
    media = {"QYWX_AM": "corp,secret,@all,10,MEDIA_ID"}
    bad = {"QYWX_AM": "corp,secret,@all"}  # 字段不足 4 个
    assert ns.enabled_channels(plain) == ["QYWX_AM"]
    assert ns.enabled_channels(media) == ["QYWX_AM"]
    assert ns.enabled_channels(bad) == []


def test_enabled_channels_explicit_disable():
    cfg = {
        "BARK_PUSH": "devkey",
        "FSKEY": "feishu",
        "PUSH_KEY": "SCTx",
        "CONSOLE": True,
        "BARK_ENABLE": False,  # 渠道名 _ENABLE 布尔禁用
        "PUSH_KEY_ENABLE": "false",  # 必需键 _ENABLE 字符串禁用
    }
    names = ns.enabled_channels(cfg)
    assert "BARK" not in names
    assert "SERVERCHAN" not in names
    assert "FEISHU" in names and "CONSOLE" in names


def test_enabled_channels_boolean_switches_and_extra_conditions():
    assert ns.enabled_channels({"CONSOLE": "false"}) == []
    assert ns.enabled_channels({"CONSOLE": "true"}) == ["CONSOLE"]
    assert ns.enabled_channels({"WXPUSHER_APP_TOKEN": "at"}) == []  # 缺 topic/uid
    assert ns.enabled_channels({"WXPUSHER_APP_TOKEN": "at", "WXPUSHER_UIDS": "u;1"}) == ["WXPUSHER"]
    assert ns.enabled_channels({"NTFY_TOPIC": "t"}) == ["NTFY"]  # NTFY_URL 缺省补 https://ntfy.sh


# ---------------------------------------------------------------------------
# Server酱新旧 key 分流
# ---------------------------------------------------------------------------


async def test_serverchan_new_and_legacy_key_routing(fake: FakeHttp):
    fake.route("push.ft07.com", httpx.Response(200, json={"errno": 0, "data": {"pushid": "1"}}))
    fake.route("sctapi.ftqq.com", httpx.Response(200, json={"errno": 0, "message": ""}))

    new_key = "sctp267205tQqExampleKey"
    results = await ns.test_push({"PUSH_KEY": new_key}, channel="SERVERCHAN")
    assert results == [("SERVERCHAN", True, "Server酱 推送成功")]
    req = fake.find_request("push.ft07.com")
    assert str(req.url) == f"https://267205.push.ft07.com/send/{new_key}.send"
    assert req.method == "POST"

    old_key = "SCT12345LegacyKey"
    results = await ns.test_push({"PUSH_KEY": old_key}, channel="SERVERCHAN")
    assert results[0][1] is True
    req = fake.find_request("sctapi.ftqq.com")
    assert str(req.url) == f"https://sctapi.ftqq.com/{old_key}.send"


async def test_serverchan_desp_markdown_and_error(fake: FakeHttp):
    results = await ns.test_push({"PUSH_KEY": "SCTx"}, channel="SERVERCHAN")
    assert results[0][1] is True

    fake.route("sctapi.ftqq.com", httpx.Response(200, json={"errno": 100, "message": "invalid key"}))
    results = await ns.test_push({"PUSH_KEY": "SCTx"}, channel="SERVERCHAN")
    assert results[0][1] is False
    assert "invalid key" in results[0][2]


async def test_serverchan_desp_explicit(fake: FakeHttp):
    """desp 支持 markdown：换行扩成空行。"""
    msg = await ns._send_serverchan("标题A", "行1\n行2", {"PUSH_KEY": "SCTx"}, None)
    assert msg == "Server酱 推送成功"
    body = urllib.parse.parse_qs(fake.requests[-1].content.decode("utf-8"))
    assert body["text"] == ["标题A"]
    assert body["desp"] == ["行1\n\n行2"]


# ---------------------------------------------------------------------------
# 自定义 WEBHOOK：占位替换 / 方法 / 内容类型
# ---------------------------------------------------------------------------


async def test_webhook_json_post_placeholder(fake: FakeHttp):
    cfg = {
        "WEBHOOK_URL": "https://hook.example.com/notify?seen=$content",
        "WEBHOOK_METHOD": "POST",
        "WEBHOOK_CONTENT_TYPE": "json",
        "WEBHOOK_BODY": "title: $title\ncontent: $content\nlevel: 3\nflag: true",
        "WEBHOOK_HEADERS": "X-Token: abc\nX-Two: b",
    }
    results = await ns.test_push(cfg, channel="WEBHOOK")
    assert results[0][1] is True, results[0][2]
    req = fake.find_request("hook.example.com")
    assert req.method == "POST"
    assert "$" not in str(req.url)  # URL 占位已替换
    assert req.headers["x-token"] == "abc"
    assert req.headers["x-two"] == "b"
    assert req.headers["content-type"].startswith("application/json")
    payload = json.loads(req.content.decode("utf-8"))
    assert payload["level"] == 3  # 数字值经 JSON 解析
    assert payload["flag"] is True
    assert "$title" not in payload["title"]


async def test_webhook_get_form_and_text(fake: FakeHttp):
    cfg_get = {
        "WEBHOOK_URL": "https://hook.example.com/get?t=$title",
        "WEBHOOK_METHOD": "get",
        "WEBHOOK_CONTENT_TYPE": "form",
        "WEBHOOK_BODY": "title: $title\nmsg: hello",
    }
    results = await ns.test_push(cfg_get, channel="WEBHOOK")
    assert results[0][1] is True
    req = fake.find_request("/get")
    assert req.method == "GET"
    assert req.content == b""  # GET 的 form body 降级为查询参数
    assert req.url.params["msg"] == "hello"
    assert "title" in str(req.url)

    cfg_text = {
        "WEBHOOK_URL": "https://hook.example.com/text",
        "WEBHOOK_METHOD": "POST",
        "WEBHOOK_CONTENT_TYPE": "text",
        "WEBHOOK_BODY": "$title: $content",
    }
    fake.route("/text", httpx.Response(500, text="boom"))
    results = await ns.test_push(cfg_text, channel="WEBHOOK")
    assert results[0][1] is False
    assert "500" in results[0][2]


async def test_webhook_requires_title_placeholder(fake: FakeHttp):
    cfg = {"WEBHOOK_URL": "https://hook.example.com/x", "WEBHOOK_METHOD": "POST", "WEBHOOK_BODY": "a: 1"}
    results = await ns.test_push(cfg, channel="WEBHOOK")
    assert results[0][1] is False
    assert "$title" in results[0][2]


# ---------------------------------------------------------------------------
# 未配置渠道 / test_push 行为 / 其他渠道抽查
# ---------------------------------------------------------------------------


async def test_unconfigured_channels_no_error(fake: FakeHttp):
    assert await ns.push_all("标题", "内容", {}) == []
    assert fake.requests == []
    results = await ns.test_push({"FSKEY": "k"}, channel="NOT_A_CHANNEL")
    assert results[0][1] is False and "未知渠道" in results[0][2]
    results = await ns.test_push({"TG_BOT_TOKEN": "1:a"}, channel="TG_BOT")
    assert results[0][1] is False and "TG_USER_ID" in results[0][2]
    results = await ns.test_push({"BARK_PUSH": "k", "BARK_ENABLE": False}, channel="BARK")
    assert results[0][1] is False and "禁用" in results[0][2]


async def test_test_push_all_channels(fake: FakeHttp):
    cfg = {"BARK_PUSH": "devkey", "IGOT_PUSH_KEY": "ig", "PUSH_KEY": "SCT1"}
    results = await ns.test_push(cfg)
    assert len(results) == 3
    assert all(ok for _, ok, _ in results)
    names = {name for name, _, _ in results}
    assert names == {"BARK", "IGOT", "SERVERCHAN"}


async def test_dingding_signature_and_feishu_body(fake: FakeHttp):
    cfg = {
        "DD_BOT_TOKEN": "ding-token",
        "DD_BOT_SECRET": "SEC123",
        "FSKEY": "feishu-key",
    }
    results = await ns.push_all("标题", "内容", cfg)
    assert {r[0]: r[1] for r in results} == {"DD_BOT": True, "FEISHU": True}
    dd_req = fake.find_request("oapi.dingtalk.com")
    assert "timestamp=" in str(dd_req.url) and "sign=" in str(dd_req.url)
    body = json.loads(dd_req.content.decode("utf-8"))
    assert body["msgtype"] == "text" and body["text"]["content"] == "标题\n\n内容"
    fs_req = fake.find_request("open.feishu.cn")
    assert json.loads(fs_req.content.decode("utf-8"))["content"]["text"] == "标题\n\n内容"


async def test_ntfy_headers_and_title_encoding(fake: FakeHttp):
    cfg = {"NTFY_TOPIC": "mytopic", "NTFY_PRIORITY": "5"}
    results = await ns.push_all("提醒标题", "提醒内容", cfg)
    assert results[0][1] is True
    req = fake.find_request("ntfy.sh/mytopic")
    assert req.headers["priority"] == "5"
    assert req.headers["title"].startswith("=?utf-8?B?")  # RFC 2047
    assert req.content.decode("utf-8") == "提醒内容"


async def test_qywx_am_upload_flow_uses_media_id(fake: FakeHttp):
    fake.route("gettoken", httpx.Response(200, json={"access_token": "tok-1", "errcode": 0}))
    fake.route("message/send", httpx.Response(200, json={"errcode": 0, "errmsg": "ok"}))
    cfg = {"QYWX_AM": "corp-id,corp-secret,@all,1000002,THUMB_MEDIA"}
    results = await ns.push_all("标题", "内容1\n内容2", cfg)
    assert results[0][0] == "QYWX_AM" and results[0][1] is True
    send_req = fake.find_request("message/send")
    body = json.loads(send_req.content.decode("utf-8"))
    assert body["msgtype"] == "mpnews"
    article = body["mpnews"]["articles"][0]
    assert article["thumb_media_id"] == "THUMB_MEDIA"
    assert article["content"] == "内容1<br/>内容2"


async def test_hitokoto_appends_content(fake: FakeHttp):
    fake.route("hitokoto", httpx.Response(200, json={"hitokoto": "人生若只如初见", "from": "测试"}))
    cfg = {"FSKEY": "k", "HITOKOTO": True}
    await ns.push_all("标题", "正文", cfg)
    feishu_req = fake.find_request("open.feishu.cn")
    text = json.loads(feishu_req.content.decode("utf-8"))["content"]["text"]
    assert "正文" in text and "人生若只如初见" in text


async def test_smtp_uses_thread_and_config_keys(monkeypatch: pytest.MonkeyPatch):
    calls: dict = {}

    def fake_sync(cfg, title, content):
        calls["cfg_keys"] = sorted(k for k in cfg if k.startswith("SMTP"))
        calls["title"] = title
        return "SMTP 邮件 推送成功"

    monkeypatch.setattr(ns, "_smtp_send_sync", fake_sync)
    cfg = {
        "SMTP_SERVER": "smtp.exmail.qq.com:465",
        "SMTP_SSL": "true",
        "SMTP_EMAIL": "a@example.com",
        "SMTP_PASSWORD": "pw",
        "SMTP_NAME": "发件人",
        "SMTP_EMAIL_TO": "b@example.com,c@example.com",
        "SMTP_NAME_TO": "小明",
    }
    results = await ns.push_all("标题", "内容", cfg)
    assert results == [("SMTP", True, "SMTP 邮件 推送成功")]
    assert calls["title"] == "标题"
    assert "SMTP_EMAIL_TO" in calls["cfg_keys"]


def test_channel_key_coverage_matches_original_protocol():
    """渠道必需键必须与原项目 push_config 键名一致。"""
    expected_keys = {
        "BARK_PUSH",
        "DD_BOT_TOKEN",
        "DD_BOT_SECRET",
        "FSKEY",
        "GOBOT_URL",
        "GOBOT_QQ",
        "GOTIFY_URL",
        "GOTIFY_TOKEN",
        "IGOT_PUSH_KEY",
        "PUSH_KEY",
        "DEER_KEY",
        "CHAT_URL",
        "CHAT_TOKEN",
        "PUSH_PLUS_TOKEN",
        "WE_PLUS_BOT_TOKEN",
        "QMSG_KEY",
        "QMSG_TYPE",
        "QYWX_AM",
        "QYWX_KEY",
        "TG_BOT_TOKEN",
        "TG_USER_ID",
        "AIBOTK_KEY",
        "AIBOTK_TYPE",
        "AIBOTK_NAME",
        "SMTP_SERVER",
        "SMTP_SSL",
        "SMTP_EMAIL",
        "SMTP_PASSWORD",
        "SMTP_NAME",
        "PUSHME_KEY",
        "CHRONOCAT_URL",
        "CHRONOCAT_QQ",
        "CHRONOCAT_TOKEN",
        "NTFY_TOPIC",
        "WXPUSHER_APP_TOKEN",
        "DODO_BOTTOKEN",
        "DODO_BOTID",
        "DODO_LANDSOURCEID",
        "DODO_SOURCEID",
        "WEBHOOK_URL",
        "WEBHOOK_METHOD",
    }
    all_required = {k for spec in ns.CHANNELS for k in spec.keys}
    assert expected_keys <= all_required
    assert any(spec.name == "CONSOLE" and spec.keys == () for spec in ns.CHANNELS)  # 布尔开关渠道
    assert len(ns.CHANNELS) == 24
