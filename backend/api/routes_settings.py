"""全局设置读写 + 调度器状态。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..api.deps import all_settings, get_setting, set_setting
from ..core.scheduler import MAIN_JOB_ID

router = APIRouter(prefix="/api/settings", tags=["settings"])

EDITABLE_KEYS = {
    "crontab",
    "push_config",
    "magic_regex",
    "source",
    "notify_enabled",
    "sign_enabled",
    "download",
}


class SettingIn(BaseModel):
    value: object


@router.get("")
async def read_settings() -> dict:
    return all_settings()


@router.get("/{key}")
async def read_one(key: str) -> dict:
    if key not in EDITABLE_KEYS:
        raise HTTPException(404, f"不允许读取/写入设置项: {key}")
    return {"key": key, "value": get_setting(key)}


@router.put("/{key}")
async def write_one(key: str, body: SettingIn) -> dict:
    if key not in EDITABLE_KEYS:
        raise HTTPException(404, f"不允许写入设置项: {key}")
    set_setting(key, body.value)
    if key == "crontab":
        from ..main import reschedule_main_job

        reschedule_main_job()
    return {"key": key, "value": get_setting(key), "job": MAIN_JOB_ID}


class NotifyTestIn(BaseModel):
    channel: str | None = None


@router.post("/notify-test")
async def notify_test(body: NotifyTestIn) -> dict:
    from ..core.logstream import hub
    from ..services.notify_service import enabled_channels, test_push

    push_config = get_setting("push_config") or {}
    log = hub.make_logger("notify-test")
    results = await test_push(push_config, channel=body.channel, log=log)
    return {
        "enabled": enabled_channels(push_config),
        "results": [{"channel": c, "ok": ok, "message": m} for c, ok, m in results],
    }
