"""资源搜索聚合：按引擎列表并发搜索，清洗出夸克分享链接并合并去重。

协议对齐已验证实现：
- PanSou: GET {server}/api/search?kw=&cloud_types=quark&res=merge&refresh= → data.merged_by_type.quark[]
  （cloud_types 发裸值，发成 JSON 串 ["quark"] 时公共站回 code=0 但一条都不给）
- kkso: GET {server}/s/<关键词>.html，服务端渲染，条目里 copyText 入参带标题+描述/链接/提取码
- CloudSaver: GET {server}/api/search?keyword=&lastMessageId= Bearer token（失效自动登录重试），
  结果在 data[].list[].cloudLinks[]（cloudType=quark）

引擎配置（多实例、启用、单源选取）在 search_engines 归一化，本模块只管打请求与合并结果。
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from .search_engines import resolve_engines

CST = timezone(timedelta(hours=8))

_PANSOU_TITLE = re.compile(r"^(.*?)(?:[【\[]?(?:简介|介绍|描述)[】\]]?[:：]?)(.*)$", re.S)
_CS_TITLE = re.compile(r"(?:名称|标题)[：:]?(.*)", re.S)
_CS_CONTENT = re.compile(r"(?:描述|简介)[：:]?(.*)(?:链接|标签)", re.S)


class SourceError(Exception):
    """搜索源返回了业务失败，reason 里带对方的话，好让用户知道是哪个源、为什么没结果。"""


async def _json(resp: httpx.Response) -> dict:
    """公共实例被限流时返回的是 HTML 错误页，别把 JSONDecodeError 的原文甩到界面上。"""
    try:
        return resp.json()
    except Exception:  # noqa: BLE001 响应根本不是 JSON，原因就是说给界面上看的
        raise SourceError(f"返回的不是 JSON（HTTP {resp.status_code}），可能被限流") from None


def _reason(exc: BaseException) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "请求超时"
    return str(exc) or type(exc).__name__


def _iso_to_cst(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(CST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


async def pansou_search(client: httpx.AsyncClient, engine: dict, keyword: str, deep: bool) -> tuple[list[dict], str]:
    server = engine["server"].rstrip("/")
    params = {"kw": keyword, "cloud_types": ["quark"], "res": "merge", "refresh": str(deep).lower()}
    resp = await client.get(f"{server}/api/search", params=params)
    result = await _json(resp)
    if result.get("code") != 0:
        raise SourceError(str(result.get("message") or "PanSou 返回异常"))
    rows = (result.get("data") or {}).get("merged_by_type", {}).get("quark", []) or []
    out = []
    for item in rows:
        link = item.get("url", "")
        note = item.get("note", "")
        if m := _PANSOU_TITLE.match(note):
            title, content = m.group(1), m.group(2)
        else:
            title, content = note, ""
        if link:
            out.append(
                {
                    "shareurl": link,
                    "taskname": title.strip(),
                    "content": content.strip(),
                    "datetime": _iso_to_cst(item.get("datetime", "")),
                    "channel": item.get("source", ""),
                }
            )
    return out, ""


async def cloudsaver_search(
    client: httpx.AsyncClient, engine: dict, keyword: str, deep: bool
) -> tuple[list[dict], str]:
    """返回 (结果, 新 token 或空串)。token 失效时自动登录重试。"""
    server = engine["server"].rstrip("/")
    if not (engine.get("username") and engine.get("password")):
        raise SourceError("未填写用户名或密码")
    headers = {
        "content-type": "application/json",
        "authorization": f"Bearer {engine.get('token', '')}",
    }
    params = {"keyword": keyword, "lastMessageId": ""}

    async def do_search() -> dict:
        resp = await client.get(f"{server}/api/search", params=params, headers=headers)
        return await _json(resp)

    new_token = ""
    result = await do_search()
    if not result.get("success") and "token" in str(result.get("message", "")):
        login = await client.post(
            f"{server}/api/user/login",
            json={"username": engine["username"], "password": engine["password"]},
            headers=headers,
        )
        data = await _json(login)
        if data.get("success"):
            new_token = (data.get("data") or {}).get("token", "")
            headers["authorization"] = f"Bearer {new_token}"
            result = await do_search()
    if not result.get("success"):
        raise SourceError(str(result.get("message") or "CloudSaver 返回异常"))

    out: list[dict] = []
    seen: set[str] = set()
    for channel in result.get("data") or []:
        for item in channel.get("list") or []:
            for link in item.get("cloudLinks") or []:
                if link.get("cloudType") != "quark" or not link.get("link"):
                    continue
                if link["link"] in seen:
                    continue
                seen.add(link["link"])
                title = item.get("title", "")
                if m := _CS_TITLE.search(title):
                    title = m.group(1)
                title = title.replace("&amp;", "&").strip()
                content = item.get("content", "")
                if m := _CS_CONTENT.search(content):
                    content = m.group(1)
                content = content.replace('<mark class="highlight">', "").replace("</mark>", "").strip()
                out.append(
                    {
                        "shareurl": link["link"],
                        "taskname": title,
                        "content": content,
                        "datetime": _iso_to_cst(item.get("pubDate", "")),
                        "channel": item.get("channelId", ""),
                    }
                )
    return out, new_token


_KKSO_SHARE = re.compile(r"copyText\(\$event,'[^']*','(https?://[^']*)','([^']*)'\)")
_KKSO_TITLE = re.compile(r'class="title"[^>]*>(.*?)</a>', re.S)
_KKSO_TIME = re.compile(r'class="type time">([^<]+)<')
_KKSO_SOURCE = re.compile(r"来源：([^<]+?)</span>")
_KKSO_HEAD_DESC = re.compile(r"^(?:资源标题|【标题】)[：:](.*?)(?:资源描述|【描述】)[：:](.*)$")
_KKSO_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _kkso_text(value: str) -> str:
    return " ".join(html.unescape(value).split())


async def kkso_search(client: httpx.AsyncClient, engine: dict, keyword: str, deep: bool) -> tuple[list[dict], str]:
    """kkso.net 没有 JSON 接口，搜索结果是服务端渲染的 /s/<关键词>.html。

    每个条目里 copyText(...) 的入参就带齐了标题+描述、分享链接、提取码，不用二跳详情页。
    """
    server = engine["server"].rstrip("/")
    resp = await client.get(f"{server}/s/{quote(keyword)}.html", headers={"User-Agent": _KKSO_UA})
    body = resp.text
    out = []
    for block in re.split(r'<div\s+class="item"', body)[1:]:
        share = _KKSO_SHARE.search(block)
        if not share:
            continue
        link, pwd = share.group(1), share.group(2)
        if "pan.quark.cn" not in link:  # 只走夸克转存，别的盘给了也存不了
            continue
        title_match = _KKSO_TITLE.search(block)
        raw_title = _kkso_text(title_match.group(1)) if title_match else link
        taskname, content = raw_title, ""
        if head := _KKSO_HEAD_DESC.search(raw_title):
            taskname, content = _kkso_text(head.group(1)), _kkso_text(head.group(2))
        time_match = _KKSO_TIME.search(block)
        source_match = _KKSO_SOURCE.search(block)
        out.append(
            {
                "shareurl": f"{link}?pwd={pwd}" if pwd else link,
                "taskname": taskname,
                "content": content,
                "datetime": _kkso_text(time_match.group(1)) if time_match else "",
                "channel": _kkso_text(source_match.group(1)) if source_match else "",
            }
        )
    if out:
        return out, ""
    # 三种「没结果」得说得不一样，否则用户分不清是关键词的问题、盘的口径的问题还是站点改版
    if "copyText(" in body:
        raise SourceError("夸克搜有结果但都不是夸克网盘，本项目转存不了")
    if "网盘接口暂时无响应" in body:
        raise SourceError("夸克搜没有这个关键词的结果")
    raise SourceError(f"没抓到条目（HTTP {resp.status_code}），可能页面结构变了")


ADAPTERS: dict = {
    "pansou": pansou_search,
    "kkso": kkso_search,
    "cloudsaver": cloudsaver_search,
}


def _share_key(url: str) -> str:
    """去重键：忽略尾斜杠与 #片段，但保留 query —— ?pwd= 提取码不同就是两条不同的分享。"""
    return url.strip().rstrip("/").split("#", 1)[0]


def _merge(rows: list[dict]) -> list[dict]:
    """同一条分享被多引擎命中时并成一条：保留先出现的清洗结果，来源并注，时间取较新。"""
    by_key: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        key = _share_key(row["shareurl"])
        if not key:
            continue
        kept = by_key.get(key)
        if kept is None:
            by_key[key] = dict(row)
            order.append(key)
            continue
        kept["datetime"] = max(kept["datetime"], row["datetime"])
        if row["source"] not in kept["source"].split(" + "):
            kept["source"] = f"{kept['source']} + {row['source']}"
    merged = [by_key[key] for key in order]
    merged.sort(key=lambda row: row["datetime"], reverse=True)  # 空时间自然沉底；同时间保持配置顺序
    return merged


async def search_all(
    query: str,
    deep: bool,
    source_cfg: dict | None,
    engine_id: str = "",
    timeout: float = 15.0,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """并发搜各引擎并合并去重。

    返回 {data, errors, token_updates}：errors 逐源说明谁没出结果，
    token_updates 是 {引擎 id: 新 token}，由调用方写回该引擎。
    """
    targets, errors = resolve_engines(source_cfg, engine_id)
    if not targets:
        return {"data": [], "errors": errors, "token_updates": {}}

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    rows: list[dict] = []
    token_updates: dict[str, str] = {}
    try:
        outs = await asyncio.gather(
            *(ADAPTERS[engine["type"]](client, engine, query, deep) for engine in targets),
            return_exceptions=True,
        )
    finally:
        if own_client:
            await client.aclose()

    for engine, out in zip(targets, outs, strict=True):
        if isinstance(out, BaseException):
            errors.append({"engine": engine["name"], "reason": _reason(out)})
            continue
        got_rows, new_token = out
        if new_token:
            token_updates[engine["id"]] = new_token
        for row in got_rows:
            row["source"] = engine["name"]
        rows.extend(got_rows)
    return {"data": _merge(rows), "errors": errors, "token_updates": token_updates}
