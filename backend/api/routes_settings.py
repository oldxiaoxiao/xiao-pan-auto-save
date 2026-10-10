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
    "notify_quiet",
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


def merge_source(value: object) -> dict:
    """source 与 task_defaults 同理：老形状在读取口径折成 {"engines": [...]}，前端和搜索只有一份答案。"""
    from ..services.search_engines import normalize_source_cfg

    return {"engines": normalize_source_cfg(value if isinstance(value, dict) else None)}


def merge_notify_quiet(value: object) -> dict:
    """免打扰配置也允许部分写入：缺的键回落默认，避免存半份把窗口算成整天。"""
    from ..services.notify_center import DEFAULT_QUIET

    base = dict(DEFAULT_QUIET)
    if isinstance(value, dict):
        base.update({k: v for k, v in value.items() if k in base})
    return base


def read_value(key: str, value: object) -> object:
    """设置值出 API 的唯一口径：旁路写进来的旧形状、部分写入都在这里被规整。"""
    if key == "task_defaults":
        return merge_task_defaults(value)
    if key == "source":
        return merge_source(value)
    if key == "notify_quiet":
        return merge_notify_quiet(value)
    return value


class SettingIn(BaseModel):
    value: object


@router.get("")
async def read_settings() -> dict:
    merged = all_settings()
    for key in ("task_defaults", "source", "notify_quiet"):
        merged[key] = read_value(key, merged.get(key))
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


# 具体路径必须排在 /{key} 之前：否则 "notify" 会被当成设置键名匹配掉
@router.get("/notify/pending")
async def notify_pending() -> dict:
    """FR-04：免打扰队列现状——攒了几条、其中几条需要处理、现在是否在窗口内。"""
    from ..services import notify_center

    settings = {"notify_quiet": get_setting("notify_quiet")}
    return {
        "ok": True,
        "quiet": notify_center.in_quiet(settings=settings),
        "count": notify_center.pending_count(),
        "action": notify_center.pending_count(notify_center.LEVEL_ACTION),
        "info": notify_center.pending_count(notify_center.LEVEL_INFO),
        "config": notify_center.quiet_config(settings),
    }


@router.post("/notify/flush")
async def notify_flush() -> dict:
    """立即补发攒下的摘要（忽略当前是否在免打扰窗口——用户点按钮就是要现在看）。"""
    from ..core.logstream import hub
    from ..services import notify_center

    settings = {
        "notify_enabled": bool(get_setting("notify_enabled")),
        "notify_quiet": get_setting("notify_quiet"),
    }
    log = hub.make_logger("notify-flush")
    sent = await notify_center.flush_pending(
        settings=settings,
        push_config=get_setting("push_config") or {},
        log=log,
        force=True,
    )
    return {"ok": True, "sent": sent}


@router.get("/{key}")
async def read_one(key: str) -> dict:
    if key not in EDITABLE_KEYS:
        raise HTTPException(404, f"不允许读取/写入设置项: {key}")
    # 读单键必须与读全量同一口径：库里若有旁路写进来的旧形状值，不规整就会给两套答案。
    return {"key": key, "value": read_value(key, get_setting(key))}


@router.put("/{key}")
async def write_one(key: str, body: SettingIn) -> dict:
    if key not in EDITABLE_KEYS:
        raise HTTPException(404, f"不允许写入设置项: {key}")
    if key in ("task_defaults", "source", "notify_quiet"):
        # 写入即规整成规范形状落库，读回来不用二次猜
        set_setting(key, read_value(key, body.value))
    else:
        set_setting(key, body.value)
    if key == "crontab":
        from ..main import reschedule_main_job

        reschedule_main_job()
    if key == "notify_quiet":
        from ..main import reschedule_notify_digest

        reschedule_notify_digest()
    return {"key": key, "value": read_value(key, get_setting(key)), "job": MAIN_JOB_ID}


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
