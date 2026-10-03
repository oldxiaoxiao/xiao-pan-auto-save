"""通知服务：多渠道路由推送模块。

clean-room 实现，仅参考 quark-auto-save/notify.py 的渠道协议（配置键名、请求方法、
URL 构造与 body 格式），未复制其代码。全部渠道键名与该项目 push_config 保持一致。

对外 API：
- ``push_all(title, content, push_config, log=None)``  并发推送所有已配置且启用的渠道
- ``enabled_channels(push_config)``                    列出将启用的渠道名（供 WebUI）
- ``test_push(push_config, channel=None, log=None)``   发送一条测试消息

渠道启用规则（与原项目一致的隐式启用风格）：
- 渠道的全部必需键均为"已配置"（非 None/空/False）即视为启用；
- 额外支持 ``"<渠道名>_ENABLE"`` 或 ``"<必需键>_ENABLE"`` 设为 False/``"false"`` 显式禁用；
- ``CONSOLE`` 等布尔开关按开关值本身真值判定（``True`` 启用，``"false"``/False 不启用）。

仅依赖 httpx 与标准库；SMTP 通过 ``asyncio.to_thread`` 包裹阻塞调用。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import smtplib
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

import httpx

LogFn = Callable[[str, str], None] | None
"""日志回调：log(level, msg)；可为 None。"""

SenderFn = Callable[[str, str, dict[str, Any], LogFn], Awaitable[str]]
"""渠道发送函数：async (title, content, push_config, log) -> 成功描述文本；失败抛 NotifyError。"""

CHANNEL_TIMEOUT = 15.0
"""单渠道推送超时（秒）。"""

_OWN_FALSE_VALUES = {"false", "0", "no", "off"}


class NotifyError(Exception):
    """渠道推送失败（协议层面返回错误码 / 配置不完整）。"""


_transport: httpx.AsyncBaseTransport | None = None
"""可注入的 httpx transport（测试用 MockTransport 拦截全部请求，不打真实网络）。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def _make_logger(log: LogFn):
    def _log(level: str, msg: str) -> None:
        if log is None:
            return
        try:
            log(level, msg)
        except Exception:  # 日志回调异常不能影响推送
            pass

    return _log


def _has_value(value: Any) -> bool:
    """键是否"已配置"：None / False / 空串 / 0 视为未配置。"""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _switch_on(value: Any) -> bool:
    """布尔开关：True/非 false 字符串视为开启。"""
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _OWN_FALSE_VALUES and bool(value.strip())
    if isinstance(value, bool):
        return value
    return bool(value)


def _as_str(value: Any) -> str:
    return "" if value is None else str(value)


async def _send(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
    json_body: Any | None = None,
    content: bytes | str | None = None,
    timeout: float = CHANNEL_TIMEOUT,
    proxy: str | None = None,
) -> httpx.Response:
    """统一 HTTP 出口：所有渠道都经此发送，便于测试注入 transport。"""
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if _transport is not None:
        client_kwargs["transport"] = _transport
    if proxy:
        client_kwargs["proxy"] = proxy
    async with httpx.AsyncClient(**client_kwargs) as client:
        return await client.request(
            method.upper(),
            url,
            params=params,
            headers=headers,
            data=data,
            json=json_body,
            content=content,
        )


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        parsed = resp.json()
    except Exception as exc:
        raise NotifyError(f"响应不是 JSON（HTTP {resp.status_code}）: {resp.text[:200]}") from exc
    if not isinstance(parsed, dict):
        raise NotifyError(f"响应 JSON 结构异常: {str(parsed)[:200]}")
    return parsed


# ---------------------------------------------------------------------------
# 各渠道发送实现（协议对照原项目，键名完全一致）
# ---------------------------------------------------------------------------


async def _send_bark(title: str, content: str, cfg: dict, log: LogFn) -> str:
    push = _as_str(cfg["BARK_PUSH"])
    url = push if push.startswith("http") else f"https://api.day.app/{push}"
    data: dict[str, Any] = {"title": title, "body": content}
    params_map = {
        "BARK_ARCHIVE": "isArchive",
        "BARK_GROUP": "group",
        "BARK_SOUND": "sound",
        "BARK_ICON": "icon",
        "BARK_LEVEL": "level",
        "BARK_URL": "url",
    }
    for key, param in params_map.items():
        if _has_value(cfg.get(key)):
            data[param] = _as_str(cfg[key])
    resp = _json(await _send("POST", url, json_body=data))
    if resp.get("code") == 200:
        return "bark 推送成功"
    raise NotifyError(f"bark 推送失败: code={resp.get('code')} message={resp.get('message')}")


async def _send_console(title: str, content: str, cfg: dict, log: LogFn) -> str:
    _make_logger(log)("INFO", f"[CONSOLE] {title}\n\n{content}")
    return "已输出到控制台日志"


async def _send_dingding(title: str, content: str, cfg: dict, log: LogFn) -> str:
    secret = _as_str(cfg["DD_BOT_SECRET"])
    token = _as_str(cfg["DD_BOT_TOKEN"])
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest))
    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}&timestamp={timestamp}&sign={sign}"
    body = {"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}}
    resp = _json(await _send("POST", url, json_body=body))
    if resp.get("errcode") == 0:
        return "钉钉机器人 推送成功"
    raise NotifyError(f"钉钉机器人 推送失败: {resp.get('errmsg')}")


async def _send_feishu(title: str, content: str, cfg: dict, log: LogFn) -> str:
    url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{_as_str(cfg['FSKEY'])}"
    body = {"msg_type": "text", "content": {"text": f"{title}\n\n{content}"}}
    resp = _json(await _send("POST", url, json_body=body))
    if resp.get("StatusCode") == 0 or resp.get("code") == 0:
        return "飞书 推送成功"
    raise NotifyError(f"飞书 推送失败: {resp}")


async def _send_go_cqhttp(title: str, content: str, cfg: dict, log: LogFn) -> str:
    base = _as_str(cfg["GOBOT_URL"])
    query = _as_str(cfg["GOBOT_QQ"])  # 形如 user_id=123 / group_id=456 的原始查询片段
    url = f"{base}?access_token={_as_str(cfg.get('GOBOT_TOKEN'))}&{query}"
    message = f"标题:{title}\n内容:{content}"
    resp = _json(await _send("GET", url, params={"message": message}))
    if resp.get("status") == "ok":
        return "go-cqhttp 推送成功"
    raise NotifyError(f"go-cqhttp 推送失败: {resp}")


async def _send_gotify(title: str, content: str, cfg: dict, log: LogFn) -> str:
    url = f"{_as_str(cfg['GOTIFY_URL'])}/message?token={_as_str(cfg['GOTIFY_TOKEN'])}"
    priority = cfg.get("GOTIFY_PRIORITY")
    data = {"title": title, "message": content, "priority": priority if priority is not None else 0}
    resp = _json(await _send("POST", url, data=data))
    if resp.get("id"):
        return f"gotify 推送成功 (id={resp['id']})"
    raise NotifyError(f"gotify 推送失败: {resp}")


async def _send_igot(title: str, content: str, cfg: dict, log: LogFn) -> str:
    url = f"https://push.hellyw.com/{_as_str(cfg['IGOT_PUSH_KEY'])}"
    resp = _json(await _send("POST", url, data={"title": title, "content": content}))
    if resp.get("ret") == 0:
        return "iGot 推送成功"
    raise NotifyError(f"iGot 推送失败: {resp.get('errMsg')}")


async def _send_serverchan(title: str, content: str, cfg: dict, log: LogFn) -> str:
    """Server 酱：sctp 开头的新版 Turbo key 走 <num>.push.ft07.com，其余走 sctapi.ftqq.com。"""
    key = _as_str(cfg["PUSH_KEY"])
    data = {"text": title, "desp": content.replace("\n", "\n\n")}  # desp 支持 markdown
    match = re.match(r"sctp(\d+)t", key)
    if match:
        url = f"https://{match.group(1)}.push.ft07.com/send/{key}.send"
    else:
        url = f"https://sctapi.ftqq.com/{key}.send"
    resp = _json(await _send("POST", url, data=data))
    if resp.get("errno") == 0 or resp.get("code") == 0:
        return "Server酱 推送成功"
    raise NotifyError(f"Server酱 推送失败: {resp.get('message')}")


async def _send_pushdeer(title: str, content: str, cfg: dict, log: LogFn) -> str:
    url = _as_str(cfg.get("DEER_URL")) or "https://api2.pushdeer.com/message/push"
    data = {"text": title, "desp": content, "type": "markdown", "pushkey": _as_str(cfg["DEER_KEY"])}
    resp = _json(await _send("POST", url, data=data))
    result = (resp.get("content") or {}).get("result") or []
    if result:
        return "PushDeer 推送成功"
    raise NotifyError(f"PushDeer 推送失败: {resp}")


async def _send_syno_chat(title: str, content: str, cfg: dict, log: LogFn) -> str:
    url = _as_str(cfg["CHAT_URL"]) + _as_str(cfg["CHAT_TOKEN"])
    payload = "payload=" + json.dumps({"text": f"{title}\n{content}"})
    resp = await _send(
        "POST",
        url,
        content=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code == 200:
        return "Synology Chat 推送成功"
    raise NotifyError(f"Synology Chat 推送失败: HTTP {resp.status_code} {resp.text[:200]}")


async def _send_pushplus(title: str, content: str, cfg: dict, log: LogFn) -> str:
    url = "https://www.pushplus.plus/send"
    data = {
        "token": _as_str(cfg["PUSH_PLUS_TOKEN"]),
        "title": title,
        "content": content,
        "topic": _as_str(cfg.get("PUSH_PLUS_USER")),
        "template": _as_str(cfg.get("PUSH_PLUS_TEMPLATE")) or "html",
        "channel": _as_str(cfg.get("PUSH_PLUS_CHANNEL")) or "wechat",
        "webhook": _as_str(cfg.get("PUSH_PLUS_WEBHOOK")),
        "callbackUrl": _as_str(cfg.get("PUSH_PLUS_CALLBACKURL")),
        "to": _as_str(cfg.get("PUSH_PLUS_TO")),
    }
    resp = _json(await _send("POST", url, json_body=data))
    code = resp.get("code")
    if code == 200:
        return f"pushplus 请求成功，流水号: {resp.get('data')}"
    raise NotifyError(f"pushplus 推送失败: code={code} msg={resp.get('msg')}")


async def _send_weplus(title: str, content: str, cfg: dict, log: LogFn) -> str:
    data = {
        "token": _as_str(cfg["WE_PLUS_BOT_TOKEN"]),
        "title": title,
        "content": content,
        "template": "html" if len(content) > 800 else "txt",
        "receiver": _as_str(cfg.get("WE_PLUS_BOT_RECEIVER")),
        "version": _as_str(cfg.get("WE_PLUS_BOT_VERSION")) or "pro",
    }
    resp = _json(await _send("POST", "https://www.weplusbot.com/send", json_body=data))
    if resp.get("code") == 200:
        return "微加机器人 推送成功"
    raise NotifyError(f"微加机器人 推送失败: {resp.get('msg')}")


async def _send_qmsg(title: str, content: str, cfg: dict, log: LogFn) -> str:
    url = f"https://qmsg.zendee.cn/{_as_str(cfg['QMSG_TYPE'])}/{_as_str(cfg['QMSG_KEY'])}"
    msg = f"{title}\n\n{content.replace('----', '-')}"
    resp = _json(await _send("POST", url, params={"msg": msg}))
    if resp.get("code") == 0:
        return "qmsg 推送成功"
    raise NotifyError(f"qmsg 推送失败: {resp.get('reason')}")


def _split_qywx_am(cfg: dict) -> list[str]:
    return [p.strip() for p in _as_str(cfg["QYWX_AM"]).split(",")]


async def _send_wecom_app(title: str, content: str, cfg: dict, log: LogFn) -> str:
    """企业微信应用：QYWX_AM = corpid,corpsecret,touser,agentid[,media_id]。

    无 media_id 时发 text；有则把该值作为图文封面 thumb_media_id 发 mpnews
    （键语义与原项目一致；不做 upload_media 上传，简化为直接使用传入的 media_id）。
    """
    parts = _split_qywx_am(cfg)
    if len(parts) < 4:
        raise NotifyError("QYWX_AM 格式错误，应为 corpid,corpsecret,touser,agentid[,media_id]")
    corpid, corpsecret, touser, agentid = parts[0], parts[1], parts[2], parts[3]
    media_id = parts[4] if len(parts) > 4 else ""
    origin = _as_str(cfg.get("QYWX_ORIGIN")) or "https://qyapi.weixin.qq.com"

    token_resp = _json(
        await _send("GET", f"{origin}/cgi-bin/gettoken", params={"corpid": corpid, "corpsecret": corpsecret})
    )
    if token_resp.get("errcode", 0) != 0 or not token_resp.get("access_token"):
        raise NotifyError(f"企业微信获取 access_token 失败: {token_resp.get('errmsg')}")
    send_url = f"{origin}/cgi-bin/message/send?access_token={token_resp['access_token']}"

    if media_id:
        body: dict[str, Any] = {
            "touser": touser,
            "msgtype": "mpnews",
            "agentid": int(agentid),
            "mpnews": {
                "articles": [
                    {
                        "title": title,
                        "thumb_media_id": media_id,
                        "author": "xiao-pan-auto-save",
                        "content_source_url": "",
                        "content": content.replace("\n", "<br/>"),
                        "digest": content,
                    }
                ]
            },
            "safe": "0",
        }
    else:
        body = {
            "touser": touser,
            "msgtype": "text",
            "agentid": int(agentid),
            "text": {"content": f"{title}\n\n{content}"},
            "safe": "0",
        }
    resp = _json(await _send("POST", send_url, json_body=body))
    if resp.get("errmsg") == "ok":
        return f"企业微信应用 推送成功（{'mpnews' if media_id else 'text'}）"
    raise NotifyError(f"企业微信应用 推送失败: {resp}")


async def _send_wecom_bot(title: str, content: str, cfg: dict, log: LogFn) -> str:
    origin = _as_str(cfg.get("QYWX_ORIGIN")) or "https://qyapi.weixin.qq.com"
    url = f"{origin}/cgi-bin/webhook/send?key={_as_str(cfg['QYWX_KEY'])}"
    body = {"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}}
    resp = _json(await _send("POST", url, json_body=body))
    if resp.get("errcode") == 0:
        return "企业微信机器人 推送成功"
    raise NotifyError(f"企业微信机器人 推送失败: {resp.get('errmsg')}")


def _telegram_proxy(cfg: dict) -> str | None:
    host = _as_str(cfg.get("TG_PROXY_HOST"))
    port = _as_str(cfg.get("TG_PROXY_PORT"))
    if not host or not port:
        return None
    auth = _as_str(cfg.get("TG_PROXY_AUTH"))
    if auth and "@" not in host:
        host = f"{auth}@{host}"
    return f"http://{host}:{port}"


async def _send_telegram(title: str, content: str, cfg: dict, log: LogFn) -> str:
    api_host = _as_str(cfg.get("TG_API_HOST")) or "https://api.telegram.org"
    url = f"{api_host}/bot{_as_str(cfg['TG_BOT_TOKEN'])}/sendMessage"
    payload = {
        "chat_id": _as_str(cfg["TG_USER_ID"]),
        "text": f"{title}\n\n{content}",
        "disable_web_page_preview": "true",
    }
    resp = _json(await _send("POST", url, data=payload, proxy=_telegram_proxy(cfg)))
    if resp.get("ok"):
        return "Telegram 推送成功"
    raise NotifyError(f"Telegram 推送失败: {resp.get('description')}")


async def _send_aibotk(title: str, content: str, cfg: dict, log: LogFn) -> str:
    a_type = _as_str(cfg["AIBOTK_TYPE"])
    message_content = f"【青龙快讯】\n\n{title}\n{content}"
    if a_type == "room":
        url = "https://api-bot.aibotk.com/openapi/v1/chat/room"
        data = {
            "apiKey": _as_str(cfg["AIBOTK_KEY"]),
            "roomName": _as_str(cfg["AIBOTK_NAME"]),
            "message": {"type": 1, "content": message_content},
        }
    else:
        url = "https://api-bot.aibotk.com/openapi/v1/chat/contact"
        data = {
            "apiKey": _as_str(cfg["AIBOTK_KEY"]),
            "name": _as_str(cfg["AIBOTK_NAME"]),
            "message": {"type": 1, "content": message_content},
        }
    resp = _json(await _send("POST", url, json_body=data))
    if resp.get("code") == 0:
        return "智能微秘书 推送成功"
    raise NotifyError(f"智能微秘书 推送失败: {resp.get('error')}")


def _smtp_send_sync(cfg: dict, title: str, content: str) -> str:
    """阻塞 SMTP 发送，由 asyncio.to_thread 包裹调用。"""
    server = _as_str(cfg["SMTP_SERVER"])
    use_ssl = _as_str(cfg.get("SMTP_SSL")).strip().lower() == "true"
    email_from = _as_str(cfg["SMTP_EMAIL"])
    password = _as_str(cfg["SMTP_PASSWORD"])
    from_name = _as_str(cfg.get("SMTP_NAME"))

    message = MIMEText(content, "plain", "utf-8")
    message["From"] = formataddr((str(Header(from_name, "utf-8")), email_from))
    to_raw = _as_str(cfg.get("SMTP_EMAIL_TO")) or email_from
    to_emails = [e.strip() for e in to_raw.split(",") if e.strip()]
    to_names = [n.strip() for n in _as_str(cfg.get("SMTP_NAME_TO")).split(",")]
    message["To"] = ",".join(
        formataddr((str(Header(to_names[i] if i < len(to_names) else "", "utf-8")), addr))
        for i, addr in enumerate(to_emails)
    )
    message["Subject"] = Header(title, "utf-8")

    host, _, port_str = server.partition(":")
    port = int(port_str) if port_str.isdigit() else (465 if use_ssl else 25)
    smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_cls(host, port, timeout=CHANNEL_TIMEOUT) as smtp_obj:
        smtp_obj.login(email_from, password)
        smtp_obj.sendmail(email_from, to_emails, message.as_bytes())
    return f"SMTP 邮件 推送成功（收件人: {', '.join(to_emails)}）"


async def _send_smtp(title: str, content: str, cfg: dict, log: LogFn) -> str:
    return await asyncio.to_thread(_smtp_send_sync, cfg, title, content)


async def _send_pushme(title: str, content: str, cfg: dict, log: LogFn) -> str:
    url = _as_str(cfg.get("PUSHME_URL")) or "https://push.i-i.me/"
    data = {
        "push_key": _as_str(cfg["PUSHME_KEY"]),
        "title": title,
        "content": content,
        "date": _as_str(cfg.get("date")),
        "type": _as_str(cfg.get("type")),
    }
    resp = await _send("POST", url, data=data)
    if resp.status_code == 200 and resp.text.strip() == "success":
        return "PushMe 推送成功"
    raise NotifyError(f"PushMe 推送失败: HTTP {resp.status_code} {resp.text[:200]}")


async def _send_chronocat(title: str, content: str, cfg: dict, log: LogFn) -> str:
    qq = _as_str(cfg["CHRONOCAT_QQ"])
    user_ids = re.findall(r"user_id=(\d+)", qq)
    group_ids = re.findall(r"group_id=(\d+)", qq)
    if not user_ids and not group_ids:
        raise NotifyError("CHRONOCAT_QQ 中未解析到 user_id= 或 group_id=")
    url = f"{_as_str(cfg['CHRONOCAT_URL'])}/api/message/send"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_as_str(cfg['CHRONOCAT_TOKEN'])}",
    }
    sent, failed = 0, []
    for chat_type, ids in ((1, user_ids), (2, group_ids)):
        for one_id in ids:
            body = {
                "peer": {"chatType": chat_type, "peerUin": one_id},
                "elements": [{"elementType": 1, "textElement": {"content": f"{title}\n\n{content}"}}],
            }
            resp = await _send("POST", url, headers=headers, content=json.dumps(body))
            if resp.status_code == 200:
                sent += 1
            else:
                failed.append(f"chatType={chat_type} id={one_id}: HTTP {resp.status_code}")
    if failed:
        raise NotifyError("CHRONOCAT 部分推送失败: " + "; ".join(failed))
    return f"CHRONOCAT 推送成功（{sent} 个目标）"


def _rfc2047(text: str) -> str:
    return "=?utf-8?B?" + base64.b64encode(text.encode("utf-8")).decode("utf-8") + "?="


async def _send_ntfy(title: str, content: str, cfg: dict, log: LogFn) -> str:
    base = _as_str(cfg.get("NTFY_URL")) or "https://ntfy.sh"
    url = f"{base}/{_as_str(cfg['NTFY_TOPIC'])}"
    headers = {
        "Title": _rfc2047(title),  # ntfy 头部需 RFC 2047 编码以支持中文
        "Priority": _as_str(cfg.get("NTFY_PRIORITY")) or "3",
        "Icon": "https://qn.whyour.cn/logo.png",
    }
    token = _as_str(cfg.get("NTFY_TOKEN"))
    username = _as_str(cfg.get("NTFY_USERNAME"))
    password = _as_str(cfg.get("NTFY_PASSWORD"))
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif username and password:
        raw = base64.b64encode(f"{username}:{password}".encode()).decode("utf-8")
        headers["Authorization"] = f"Basic {raw}"
    actions = _as_str(cfg.get("NTFY_ACTIONS"))
    if actions:
        headers["Actions"] = _rfc2047(actions)
    resp = await _send("POST", url, headers=headers, content=content.encode("utf-8"))
    if resp.status_code == 200:
        return "ntfy 推送成功"
    raise NotifyError(f"ntfy 推送失败: HTTP {resp.status_code} {resp.text[:200]}")


async def _send_dodo(title: str, content: str, cfg: dict, log: LogFn) -> str:
    bot_id = _as_str(cfg["DODO_BOTID"])
    token = _as_str(cfg["DODO_BOTTOKEN"])
    url = "https://botopen.imdodo.com/api/v2/personal/message/send"
    headers = {
        "Authorization": f"Bot {bot_id}.{token}",
        "Content-Type": "application/json",
    }
    body = {
        "islandSourceId": _as_str(cfg["DODO_LANDSOURCEID"]),
        "dodoSourceId": _as_str(cfg["DODO_SOURCEID"]),
        "messageType": 1,
        "messageBody": {"content": f"{title}\n\n{content}"},
    }
    resp = await _send("POST", url, headers=headers, json_body=body)
    if resp.status_code != 200:
        raise NotifyError(f"DoDo 推送失败: HTTP {resp.status_code} {resp.text[:200]}")
    data = _json(resp)
    if data.get("status") == 0 and data.get("message") == "success":
        return "DoDo 推送成功"
    raise NotifyError(f"DoDo 推送失败: {data}")


def _wxpusher_targets(cfg: dict) -> tuple[list[int], list[str]]:
    topic_ids = [int(p.strip()) for p in _as_str(cfg.get("WXPUSHER_TOPIC_IDS")).split(";") if p.strip()]
    uids = [p.strip() for p in _as_str(cfg.get("WXPUSHER_UIDS")).split(";") if p.strip()]
    return topic_ids, uids


async def _send_wxpusher(title: str, content: str, cfg: dict, log: LogFn) -> str:
    topic_ids, uids = _wxpusher_targets(cfg)
    url = "https://wxpusher.zjiecode.com/api/send/message"
    body = {
        "appToken": _as_str(cfg["WXPUSHER_APP_TOKEN"]),
        "content": f"<h1>{title}</h1><br/><div style='white-space: pre-wrap;'>{content}</div>",
        "summary": title[:64],
        "contentType": 2,  # HTML
        "topicIds": topic_ids,
        "uids": uids,
        "verifyPayType": 0,
    }
    resp = _json(await _send("POST", url, json_body=body))
    if resp.get("code") == 1000:
        return "wxpusher 推送成功"
    raise NotifyError(f"wxpusher 推送失败: {resp.get('msg')}")


def _parse_webhook_headers(raw: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in raw.splitlines():
        idx = line.find(":")
        if idx == -1:
            continue
        key = line[:idx].strip().lower()
        val = line[idx + 1 :].strip()
        parsed[key] = f"{parsed[key]}, {val}" if key in parsed else val
    return parsed


def _parse_webhook_body(raw: str) -> dict[str, Any]:
    """按 "key: value" 逐条解析；值先做 $title/$content 替换再尝试 JSON 解析。"""
    matches: dict[str, Any] = {}
    for m in re.finditer(r"(\w+):\s*((?:(?!\n\w+:).)*)", raw, flags=re.DOTALL):
        key, value = m.group(1).strip(), m.group(2).strip()
        try:
            matches[key] = json.loads(value)
        except (ValueError, TypeError):
            matches[key] = value
    return matches


_WEBHOOK_CT_ALIASES = {
    "form": "application/x-www-form-urlencoded",
    "json": "application/json",
    "text": "text/plain",
}


def _normalize_content_type(raw: str) -> str:
    low = raw.strip().lower()
    return _WEBHOOK_CT_ALIASES.get(low, low)


async def _send_webhook(title: str, content: str, cfg: dict, log: LogFn) -> str:
    """自定义通知：WEBHOOK_URL/HEADERS/BODY/METHOD/CONTENT_TYPE。

    - body/URL 支持 $title/$content 占位替换（body 中换行转义为字面 \\n，URL 中 quote_plus）；
    - METHOD 支持 GET/POST 等；
    - CONTENT_TYPE 支持 form/json/text（亦支持直接写 MIME）。
    """
    url_raw = _as_str(cfg["WEBHOOK_URL"])
    method = _as_str(cfg["WEBHOOK_METHOD"]).strip().upper()
    content_type = _normalize_content_type(_as_str(cfg.get("WEBHOOK_CONTENT_TYPE")))
    body_raw = _as_str(cfg.get("WEBHOOK_BODY"))

    if "$title" not in url_raw and "$title" not in body_raw:
        raise NotifyError("WEBHOOK_URL 或 WEBHOOK_BODY 中必须包含 $title 占位符")

    def substitute(value: str) -> str:
        return value.replace("$title", title.replace("\n", "\\n")).replace(
            "$content", content.replace("\n", "\\n")
        )

    headers = _parse_webhook_headers(_as_str(cfg.get("WEBHOOK_HEADERS")))
    formatted_url = url_raw.replace("$title", urllib.parse.quote_plus(title)).replace(
        "$content", urllib.parse.quote_plus(content)
    )

    params = None
    data = None
    json_body = None
    text_content = None
    if body_raw:
        if content_type == "text/plain":
            text_content = substitute(body_raw)
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        elif content_type == "application/json":
            json_body = {
                k: substitute(v) if isinstance(v, str) else v
                for k, v in _parse_webhook_body(body_raw).items()
            }
            headers.setdefault("Content-Type", "application/json; charset=utf-8")
        else:  # 默认 / form：x-www-form-urlencoded
            parsed_form = {
                k: substitute(v) if isinstance(v, str) else v
                for k, v in _parse_webhook_body(body_raw).items()
            }
            if method in ("GET", "HEAD"):
                params = parsed_form  # GET 无请求体，退化为查询参数
            else:
                data = parsed_form

    resp = await _send(
        method,
        formatted_url,
        params=params,
        headers=headers,
        data=data,
        json_body=json_body,
        content=text_content,
    )
    if resp.status_code == 200:
        return f"自定义 WEBHOOK 推送成功（{method} {formatted_url.split('?')[0]}）"
    raise NotifyError(f"自定义 WEBHOOK 推送失败: HTTP {resp.status_code} {resp.text[:200]}")


# ---------------------------------------------------------------------------
# 渠道注册表
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelSpec:
    """渠道定义：名称、必需配置键、发送函数、额外可用性判定。"""

    name: str
    keys: tuple[str, ...]
    sender: SenderFn
    predicate: Callable[[dict[str, Any]], bool] | None = None
    doc: str = ""


CHANNELS: tuple[ChannelSpec, ...] = (
    ChannelSpec("BARK", ("BARK_PUSH",), _send_bark, doc="bark iOS 推送，JSON POST"),
    ChannelSpec(
        "CONSOLE",
        (),
        _send_console,
        predicate=lambda cfg: _switch_on(cfg.get("CONSOLE")),
        doc="控制台/日志输出（布尔开关）",
    ),
    ChannelSpec("DD_BOT", ("DD_BOT_TOKEN", "DD_BOT_SECRET"), _send_dingding, doc="钉钉机器人，HMAC 签名"),
    ChannelSpec("FEISHU", ("FSKEY",), _send_feishu, doc="飞书机器人 webhook"),
    ChannelSpec("GO_CQHTTP", ("GOBOT_URL", "GOBOT_QQ"), _send_go_cqhttp, doc="go-cqhttp GET 推送"),
    ChannelSpec("GOTIFY", ("GOTIFY_URL", "GOTIFY_TOKEN"), _send_gotify, doc="gotify 自建推送"),
    ChannelSpec("IGOT", ("IGOT_PUSH_KEY",), _send_igot, doc="iGot 聚合推送，form POST"),
    ChannelSpec("SERVERCHAN", ("PUSH_KEY",), _send_serverchan, doc="Server酱（兼容 Turbo sctp 新版 key）"),
    ChannelSpec("PUSHDEER", ("DEER_KEY",), _send_pushdeer, doc="PushDeer，markdown"),
    ChannelSpec("CHAT", ("CHAT_URL", "CHAT_TOKEN"), _send_syno_chat, doc="群晖 Synology Chat"),
    ChannelSpec("PUSH_PLUS", ("PUSH_PLUS_TOKEN",), _send_pushplus, doc="pushplus，JSON POST，含全部附属键"),
    ChannelSpec("WE_PLUS_BOT", ("WE_PLUS_BOT_TOKEN",), _send_weplus, doc="微加机器人"),
    ChannelSpec("QMSG", ("QMSG_KEY", "QMSG_TYPE"), _send_qmsg, doc="qmsg 推送"),
    ChannelSpec(
        "QYWX_AM",
        ("QYWX_AM",),
        _send_wecom_app,
        predicate=lambda cfg: len(_split_qywx_am(cfg)) in (4, 5),
        doc="企业微信应用，QYWX_AM=corpid,corpsecret,touser,agentid[,media_id]",
    ),
    ChannelSpec("QYWX_KEY", ("QYWX_KEY",), _send_wecom_bot, doc="企业微信机器人 webhook"),
    ChannelSpec("TG_BOT", ("TG_BOT_TOKEN", "TG_USER_ID"), _send_telegram, doc="Telegram 机器人，支持代理"),
    ChannelSpec("AIBOTK", ("AIBOTK_KEY", "AIBOTK_TYPE", "AIBOTK_NAME"), _send_aibotk, doc="智能微秘书"),
    ChannelSpec(
        "SMTP",
        ("SMTP_SERVER", "SMTP_SSL", "SMTP_EMAIL", "SMTP_PASSWORD", "SMTP_NAME"),
        _send_smtp,
        doc="SMTP 邮件，asyncio.to_thread 包裹",
    ),
    ChannelSpec("PUSHME", ("PUSHME_KEY",), _send_pushme, doc="PushMe"),
    ChannelSpec(
        "CHRONOCAT",
        ("CHRONOCAT_URL", "CHRONOCAT_QQ", "CHRONOCAT_TOKEN"),
        _send_chronocat,
        doc="Chronocat QQ 推送",
    ),
    ChannelSpec("NTFY", ("NTFY_TOPIC",), _send_ntfy, doc="ntfy 主题推送"),
    ChannelSpec(
        "WXPUSHER",
        ("WXPUSHER_APP_TOKEN",),
        _send_wxpusher,
        predicate=lambda cfg: any(_wxpusher_targets(cfg)),
        doc="wxpusher，需 topic_ids 或 uids 至少其一",
    ),
    ChannelSpec(
        "DODO",
        ("DODO_BOTTOKEN", "DODO_BOTID", "DODO_LANDSOURCEID", "DODO_SOURCEID"),
        _send_dodo,
        doc="DoDo 机器人",
    ),
    ChannelSpec("WEBHOOK", ("WEBHOOK_URL", "WEBHOOK_METHOD"), _send_webhook, doc="自定义 webhook 通知"),
)

_CHANNELS_BY_NAME: dict[str, ChannelSpec] = {spec.name: spec for spec in CHANNELS}

# 原项目显式忽略项与辅助键（不参与渠道启用判定，仅作说明）
IGNORED_KEYS = ("QUARK_SIGN_NOTIFY",)


def _disable_flags(spec: ChannelSpec) -> tuple[str, ...]:
    flags = {f"{spec.name}_ENABLE"}
    flags.update(f"{key}_ENABLE" for key in spec.keys)
    return tuple(flags)


def _is_disabled(cfg: dict[str, Any], spec: ChannelSpec) -> bool:
    for flag in _disable_flags(spec):
        value = cfg.get(flag)
        if value is False or (isinstance(value, str) and value.strip().lower() in _OWN_FALSE_VALUES):
            return True
    return False


def _channel_ready(cfg: dict[str, Any], spec: ChannelSpec) -> bool:
    if _is_disabled(cfg, spec):
        return False
    if not all(_has_value(cfg.get(key)) for key in spec.keys):
        return False
    if spec.predicate is not None and not spec.predicate(cfg):
        return False
    return True


def enabled_channels(push_config: dict[str, Any]) -> list[str]:
    """列出将启用的渠道名，供 WebUI 展示。"""
    return [spec.name for spec in CHANNELS if _channel_ready(push_config, spec)]


# ---------------------------------------------------------------------------
# 推送主流程
# ---------------------------------------------------------------------------


def _skip_titles(cfg: dict[str, Any]) -> list[str]:
    raw = cfg.get("SKIP_PUSH_TITLE")
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [line.strip() for line in str(raw).splitlines() if line.strip()]


async def _append_hitokoto(cfg: dict[str, Any], content: str, log: Callable[[str, str], None]) -> str:
    if not _switch_on(cfg.get("HITOKOTO")):
        return content
    try:
        resp = await _send("GET", "https://v1.hitokoto.cn/")
        data = resp.json()
        return f"{content}\n\n{data['hitokoto']}    ----{data['from']}"
    except Exception as exc:
        log("WARNING", f"[HITOKOTO] 一言获取失败，忽略: {exc}")
        return content


async def _run_channel(
    spec: ChannelSpec,
    title: str,
    content: str,
    cfg: dict[str, Any],
    log: Callable[[str, str], None],
) -> tuple[str, bool, str]:
    log("INFO", f"[{spec.name}] 开始推送")
    try:
        message = await asyncio.wait_for(spec.sender(title, content, cfg, log), timeout=CHANNEL_TIMEOUT)
    except NotifyError as exc:
        log("ERROR", f"[{spec.name}] 推送失败: {exc}")
        return spec.name, False, str(exc)
    except TimeoutError:
        log("ERROR", f"[{spec.name}] 推送超时（>{CHANNEL_TIMEOUT:.0f}s）")
        return spec.name, False, f"推送超时（>{CHANNEL_TIMEOUT:.0f}s）"
    except httpx.HTTPError as exc:
        log("ERROR", f"[{spec.name}] 网络错误: {exc}")
        return spec.name, False, f"网络错误: {exc}"
    except Exception as exc:  # 单渠道失败不影响其他渠道
        log("ERROR", f"[{spec.name}] 未预期异常: {exc!r}")
        return spec.name, False, f"未预期异常: {exc!r}"
    log("INFO", f"[{spec.name}] {message}")
    return spec.name, True, message


async def _push_specs(
    specs: list[ChannelSpec],
    title: str,
    content: str,
    cfg: dict[str, Any],
    log: Callable[[str, str], None],
) -> list[tuple[str, bool, str]]:
    results = await asyncio.gather(*(_run_channel(spec, title, content, cfg, log) for spec in specs))
    return list(results)


async def push_all(
    title: str,
    content: str,
    push_config: dict[str, Any],
    log: LogFn = None,
) -> list[tuple[str, bool, str]]:
    """并发向所有已配置且启用的渠道推送。

    返回每渠道结果 ``(channel, ok, message)``；单渠道失败（含超时/异常）不影响其他渠道。
    """
    logf = _make_logger(log)
    if not content or not str(content).strip():
        logf("WARNING", "推送内容为空，跳过全部渠道")
        return []
    if title in _skip_titles(push_config):
        logf("INFO", f"标题 {title!r} 命中 SKIP_PUSH_TITLE，跳过推送")
        return []
    specs = [spec for spec in CHANNELS if _channel_ready(push_config, spec)]
    if not specs:
        logf("WARNING", "未配置任何可用通知渠道，跳过推送")
        return []
    final_content = await _append_hitokoto(push_config, content, logf)
    logf("INFO", f"开始并发推送 {len(specs)} 个渠道: {', '.join(s.name for s in specs)}")
    return await _push_specs(specs, title, final_content, push_config, logf)


async def test_push(
    push_config: dict[str, Any],
    channel: str | None = None,
    log: LogFn = None,
) -> list[tuple[str, bool, str]]:
    """向指定渠道（或全部启用渠道）发送一条测试消息。"""
    logf = _make_logger(log)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    title = "【测试】小盘通知测试"
    content = f"这是一条测试消息，用于验证通知渠道配置是否正确。\n时间：{now}"

    if channel is not None:
        spec = _CHANNELS_BY_NAME.get(channel.strip().upper())
        if spec is None:
            return [(channel, False, f"未知渠道: {channel}")]
        missing = [key for key in spec.keys if not _has_value(push_config.get(key))]
        if missing:
            return [(spec.name, False, f"缺少必需配置键: {', '.join(missing)}")]
        if _is_disabled(push_config, spec):
            return [(spec.name, False, "渠道已被 *_ENABLE=False 显式禁用")]
        if spec.predicate is not None and not spec.predicate(push_config):
            return [(spec.name, False, "渠道配置不完整（附加条件不满足）")]
        return await _push_specs([spec], title, content, push_config, logf)
    return await push_all(title, content, push_config, log)


__all__ = [
    "CHANNEL_TIMEOUT",
    "CHANNELS",
    "NotifyError",
    "enabled_channels",
    "push_all",
    "test_push",
]
