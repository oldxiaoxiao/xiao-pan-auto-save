"""管理界面鉴权；外部 API 仍由自己的 Token 依赖保护。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import config

router = APIRouter(prefix="/api/auth", tags=["auth"])
COOKIE = "xiao_pan_session"
SESSION_SECONDS = 7 * 24 * 3600
_secret = secrets.token_bytes(32)
_PUBLIC = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout", "/api/auth/desktop"}
_EXTERNAL = {"/api/add_task", "/api/tasklist", "/api/v1/task/add", "/api/v1/task/list", "/api/v1/task/update", "/api/v1/task/run"}


def _signature(issued: str) -> str:
    message = f"{issued}\0{config.WEBUI_USERNAME}\0{config.WEBUI_PASSWORD}".encode()
    return hmac.new(_secret, message, hashlib.sha256).hexdigest()


def authenticated(request: Request) -> bool:
    if not config.WEBUI_PASSWORD:
        return True
    cookie = request.cookies.get(COOKIE, "")
    issued, _, signature = cookie.partition(".")
    try:
        age = time.time() - int(issued)
        if age >= 0 and (config.DESKTOP_MODE or age < SESSION_SECONDS) and secrets.compare_digest(signature, _signature(issued)):
            return True
    except (ValueError, TypeError):
        pass
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            username, _, password = base64.b64decode(header[6:], validate=True).decode().partition(":")
            return _credentials_match(username, password)
        except (ValueError, UnicodeError, binascii.Error):
            pass
    return False


def _credentials_match(username: str, password: str) -> bool:
    return secrets.compare_digest(username.encode(), config.WEBUI_USERNAME.encode()) and secrets.compare_digest(password.encode(), config.WEBUI_PASSWORD.encode())


def _session(response: Response, request: Request) -> Response:
    issued = str(int(time.time()))
    response.set_cookie(COOKIE, f"{issued}.{_signature(issued)}", max_age=None if config.DESKTOP_MODE else SESSION_SECONDS,
                        httponly=True, samesite="strict", secure=request.url.scheme == "https")
    response.headers["Cache-Control"] = "no-store"
    return response


class WebAuthMiddleware:
    """纯 ASGI 中间件保留 SSE 流行为，避免对流式响应做缓冲。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = request.url.path.rstrip("/") or "/"
        if path not in _EXTERNAL:
            origin = request.headers.get("origin")
            if request.method not in {"GET", "HEAD", "OPTIONS"} and origin and urlsplit(origin).netloc != request.headers.get("host"):
                await JSONResponse({"detail": "不允许跨站管理请求"}, status_code=403)(scope, receive, send)
                return
            protected = path.startswith("/api/") or path in {"/docs", "/redoc", "/openapi.json"}
            if protected and path not in _PUBLIC and not authenticated(request):
                await JSONResponse({"detail": "请先登录管理界面"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


@router.get("/status")
async def status(request: Request) -> dict:
    return {"required": bool(config.WEBUI_PASSWORD) and not config.DESKTOP_MODE, "authenticated": authenticated(request)}


class LoginIn(BaseModel):
    username: str = "admin"
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request) -> Response:
    if config.WEBUI_PASSWORD and not _credentials_match(body.username, body.password):
        raise HTTPException(401, "用户名或密码错误")
    return _session(JSONResponse({"ok": True}), request)


@router.post("/logout")
async def logout() -> Response:
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/desktop")
async def desktop_login(request: Request, token: str = "") -> Response:
    if not config.DESKTOP_TOKEN or not secrets.compare_digest(token.encode(), config.DESKTOP_TOKEN.encode()):
        raise HTTPException(401, "客户端启动凭证无效")
    config.DESKTOP_TOKEN = ""  # 每次启动只允许交换一次，凭证不留在后续页面 URL。
    return _session(RedirectResponse("/", status_code=303), request)


@router.post("/desktop-stop")
async def desktop_stop(request: Request) -> dict:
    stop = getattr(request.app.state, "desktop_shutdown", None)
    if not config.DESKTOP_MODE or stop is None:
        raise HTTPException(404, "非客户端服务")
    stop()
    return {"ok": True}
