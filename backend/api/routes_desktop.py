"""桌面外壳接口：开机自启与系统通知。

这两项只在客户端（原生窗口）里成立；Compose / 服务端部署时接口如实返回 supported=False，
由界面隐藏开关并说明原因——不给一个按下去没用的控件。
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/desktop", tags=["desktop"])


class AutostartIn(BaseModel):
    enabled: bool


class NotifyIn(BaseModel):
    title: str = "小盘自动转存"
    message: str = Field(default="", max_length=500)


@router.get("/autostart")
async def get_autostart() -> dict:
    from ..services.desktop_shell import autostart_state

    return {"ok": True, "data": autostart_state()}


@router.put("/autostart")
async def put_autostart(body: AutostartIn) -> dict:
    from ..services.desktop_shell import set_autostart

    return set_autostart(body.enabled)


@router.post("/notify")
async def post_notify(body: NotifyIn) -> dict:
    from ..services.desktop_shell import notify

    return notify(body.title, body.message)
