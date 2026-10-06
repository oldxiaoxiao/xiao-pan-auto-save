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
    "task_defaults",
}


def merge_task_defaults(value: object) -> dict:
    """task_defaults 允许部分写入：缺的键回落到 DEFAULT_SETTINGS 的那一份，避免存半份就抹掉别的默认。"""
    from ..api.deps import DEFAULT_SETTINGS

    base = dict(DEFAULT_SETTINGS["task_defaults"])
    if isinstance(value, dict):
        base.update({k: v for k, v in value.items() if k in base})
    return base


class SettingIn(BaseModel):
    value: object


@router.get("")
async def read_settings() -> dict:
    merged = all_settings()
    merged["task_defaults"] = merge_task_defaults(merged.get("task_defaults"))
    return merged


@router.get("/magic/expand")
async def expand_magic(name: str) -> dict:
    """把 $TV 这类魔法关键字展开成真实正则给前端显示；展开逻辑只有 MagicRename 一处实现。"""
    from ..core.magic import MagicRename

    mr = MagicRename(get_setting("magic_regex") or {})
    pattern, replace = mr.magic_regex_conv(name, "")
    if pattern == name:  # 没命中关键字：原样返回，前端据此判定不是预设
        return {"ok": False, "name": name, "pattern": "", "replace": ""}
    return {"ok": True, "name": name, "pattern": pattern, "replace": replace}


@router.get("/{key}")
async def read_one(key: str) -> dict:
    if key not in EDITABLE_KEYS:
        raise HTTPException(404, f"不允许读取/写入设置项: {key}")
    value = get_setting(key)
    if key == "task_defaults":
        # 读单键必须与读全量同一口径：库里若有旁路写进来的半份 task_defaults，
        # 不 merge 就会让 GET /api/settings 给六键、GET /api/settings/task_defaults 给残缺——第二套答案。
        value = merge_task_defaults(value)
    return {"key": key, "value": value}


@router.put("/{key}")
async def write_one(key: str, body: SettingIn) -> dict:
    if key not in EDITABLE_KEYS:
        raise HTTPException(404, f"不允许写入设置项: {key}")
    if key == "task_defaults":
        set_setting(key, merge_task_defaults(body.value))
    else:
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
