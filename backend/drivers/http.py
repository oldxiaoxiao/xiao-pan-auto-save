"""驱动共用 HTTP 客户端：httpx 封装、统一请求头、退避重试、日志钩子。"""

from __future__ import annotations

import asyncio
import json as jsonlib
from collections.abc import Callable
from typing import Any

import httpx

from ..config import REQUEST_TIMEOUT

LogFn = Callable[[str, str], None]


class DriveHttpClient:
    def __init__(
        self,
        headers: dict[str, str] | None = None,
        proxy: str = "",
        timeout: float = REQUEST_TIMEOUT,
        max_retries: int = 3,
        log: LogFn | None = None,
    ):
        self._client = httpx.AsyncClient(
            headers=headers or {},
            proxy=proxy or None,
            timeout=timeout,
            follow_redirects=True,
        )
        self.max_retries = max_retries
        self.log = log or (lambda level, msg: None)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        with_cookies: bool = False,
    ) -> Any:
        """发起请求，返回解析后的 JSON dict；with_cookies=True 时返回 (json, set_cookie_str)。

        网络错误/HTTP>=500 按 1s、2s、4s 退避重试；最终失败抛异常。
        业务错误由调用方按响应体 code 判断。
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = await self._client.request(method, url, params=params, json=json, headers=headers)
                if resp.status_code >= 500:
                    last_exc = httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}", request=resp.request, response=resp
                    )
                    self.log("warn", f"HTTP {resp.status_code}，重试 {attempt + 1}/{self.max_retries}")
                    await asyncio.sleep(2**attempt)
                    continue
                try:
                    body = resp.json()
                except jsonlib.JSONDecodeError:
                    body = {"__raw__": resp.text, "status": resp.status_code}
                if with_cookies:
                    return body, "; ".join(f"{k}={v}" for k, v in resp.cookies.items())
                return body
            except httpx.HTTPError as exc:
                last_exc = exc
                self.log("warn", f"网络异常({exc.__class__.__name__})，重试 {attempt + 1}/{self.max_retries}")
                await asyncio.sleep(2**attempt)
        raise ConnectionError(f"请求失败: {method} {url} -> {last_exc}")

    async def get(self, url: str, **kw: Any) -> dict:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> dict:
        return await self.request("POST", url, **kw)

    async def aclose(self) -> None:
        await self._client.aclose()
