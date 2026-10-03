"""资源搜索聚合：PanSou / CloudSaver 两种搜索源，清洗出夸克分享链接。

协议对齐已验证实现：
- PanSou: GET {server}/api/search?kw=&cloud_types=[quark]&res=merge&refresh= → data.merged_by_type.quark[]
- CloudSaver: GET {server}/api/search?keyword=&lastMessageId= Bearer token（失效自动登录重试），
  结果在 data[].list[].cloudLinks[]（cloudType=quark）
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import httpx

CST = timezone(timedelta(hours=8))

_PANSOU_TITLE = re.compile(r"^(.*?)(?:[【\[]?(?:简介|介绍|描述)[】\]]?[:：]?)(.*)$", re.S)
_CS_TITLE = re.compile(r"(?:名称|标题)[：:]?(.*)", re.S)
_CS_CONTENT = re.compile(r"(?:描述|简介)[：:]?(.*)(?:链接|标签)", re.S)


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


async def pansou_search(client: httpx.AsyncClient, server: str, keyword: str, refresh: bool) -> list[dict]:
    url = f"{server.rstrip('/')}/api/search"
    params = {"kw": keyword, "cloud_types": ["quark"], "res": "merge", "refresh": str(refresh).lower()}
    try:
        resp = await client.get(url, params=params)
        result = resp.json()
    except Exception:  # noqa: BLE001 搜索源失败静默降级
        return []
    if result.get("code") != 0:
        return []
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
                    "source": "PanSou",
                }
            )
    return out


async def cloudsaver_search(client: httpx.AsyncClient, cfg: dict, keyword: str) -> tuple[list[dict], str]:
    """返回 (结果, 新token或空串)。token 失效时自动登录重试。"""
    server = (cfg.get("server") or "").rstrip("/")
    if not (server and cfg.get("username") and cfg.get("password")):
        return [], ""
    headers = {"content-type": "application/json", "authorization": f"Bearer {cfg.get('token', '')}"}
    params = {"keyword": keyword, "lastMessageId": ""}

    async def do_search() -> dict:
        resp = await client.get(f"{server}/api/search", params=params, headers=headers)
        return resp.json()

    new_token = ""
    try:
        result = await do_search()
        if not result.get("success") and "token" in str(result.get("message", "")):
            login = await client.post(
                f"{server}/api/user/login",
                json={"username": cfg["username"], "password": cfg["password"]},
                headers=headers,
            )
            data = login.json()
            if data.get("success"):
                new_token = (data.get("data") or {}).get("token", "")
                headers["authorization"] = f"Bearer {new_token}"
                result = await do_search()
    except Exception:  # noqa: BLE001
        return [], ""
    if not result.get("success"):
        return [], ""

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
                        "source": "CloudSaver",
                    }
                )
    return out, new_token


async def search_all(
    query: str, deep: bool, source_cfg: dict, timeout: float = 15.0, client: httpx.AsyncClient | None = None
) -> dict:
    """聚合搜索并去重排序；返回 {data, new_cs_token}。client 可注入（测试用）。"""
    results: list[dict] = []
    new_token = ""
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        tasks = []
        ps_cfg = source_cfg.get("pansou") or {}
        cs_cfg = source_cfg.get("cloudsaver") or {}
        if ps_cfg.get("server", True) and str(ps_cfg.get("enable", "true")).lower() != "false":
            tasks.append(
                pansou_search(client, str(ps_cfg.get("server") or "https://so.252035.xyz"), query, deep)
            )
        if str(cs_cfg.get("enable", "true")).lower() != "false" and cs_cfg.get("server"):
            tasks.append(cloudsaver_search(client, cs_cfg, query))
        for coro in tasks:
            res = await coro
            if isinstance(res, tuple):
                rows, new_token = res
            else:
                rows = res
            results.extend(rows)
    finally:
        if own_client:
            await client.aclose()

    seen: set[str] = set()
    unique = []
    for item in results:
        if item["shareurl"] and item["shareurl"] not in seen:
            seen.add(item["shareurl"])
            unique.append(item)
    unique.sort(key=lambda x: x.get("datetime", ""), reverse=True)
    return {"data": unique, "new_cs_token": new_token}
