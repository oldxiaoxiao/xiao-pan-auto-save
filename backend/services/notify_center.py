"""通知分级与免打扰（主方案 FR-04）。

过去通知只有一种语气：转存成功和"Cookie 失效、磁盘满了"混在同一条消息里发出去，
用户要么被成功消息淹掉真正要处理的事，要么索性把通知整个关掉。这里三件事：

1. 分级：需处理（action）与仅告知（info）分开成两条消息，标题带级别标记；
2. 免打扰：时段内**不丢**需处理事件，攒进 notify_pending，时段结束后合成一条摘要补发；
3. 按任务静音：仅告知级可以按任务关掉（追更成功的剧不想每条都收到），需处理级永不静音。

对外 API：
- ``line(text, level=, task_id=)``         构造带级别的通知行（str 子类，可直接 join）
- ``classify(text)``                       没标级别时按内容兜底判定
- ``dispatch(items, ...)``                 分级 + 静音 + 免打扰入队 / 直发
- ``flush_pending(...)``                   免打扰结束后的聚合摘要补发
- ``quiet_config(settings)`` / ``in_quiet()``  免打扰窗口判定
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import select

from ..database import session_scope
from ..models import NotifyPending, Task

LEVEL_ACTION = "action"
LEVEL_INFO = "info"

LEVEL_LABEL = {LEVEL_ACTION: "需处理", LEVEL_INFO: "仅告知"}
LEVEL_ICON = {LEVEL_ACTION: "⚠️", LEVEL_INFO: "ℹ️"}

DEFAULT_QUIET = {"enabled": True, "start": "23:00", "end": "08:00"}
"""免打扰默认 23:00–08:00（需求口径）。"""

# 摘要补发比免打扰结束晚 5 分钟：整点那一刻往往正好撞上主运行，晚一点避免同一分钟里连发两条
DIGEST_DELAY_MINUTES = 5

MAX_PENDING = 200
"""待发队列上限：通知关掉或渠道长期不通时不能让表无限长。"""

# 兜底判级用的线索。调用方最好显式标级别，这里只兜住"没标"的老字符串
_ACTION_PREFIXES = ("❌", "🔑", "💾", "⚠️")
_ACTION_KEYWORDS = (
    "磁盘空间不足",
    "网盘容量不足",
    "Cookie 已失效",
    "没有支持该链接",
    "未配置可用",
    "限流",
    "风控",
    "重试用尽",
)


class NotifyLine(str):
    """带级别与任务归属的通知行。

    刻意继承 str：历史代码里 notify_lines 一直是 list[str]，`"\n".join(...)`、
    `startswith("✅")`、测试里的 `in` 比较都还能照原样工作，只是多挂了两个属性。
    """

    level: str = LEVEL_INFO
    task_id: int | None = None

    def __new__(cls, text: str, level: str | None = None, task_id: int | None = None):
        obj = super().__new__(cls, text)
        obj.level = level or classify(text)
        obj.task_id = task_id
        return obj


def line(text: str, level: str | None = None, task_id: int | None = None) -> NotifyLine:
    """构造通知行；level 为空时按内容兜底判级。"""
    return NotifyLine(text, level=level, task_id=task_id)


def classify(text: str) -> str:
    """按内容判级：拿不到显式级别时的兜底（老调用方、外部拼的字符串）。"""
    raw = (text or "").strip()
    if raw.startswith(_ACTION_PREFIXES):
        return LEVEL_ACTION
    if any(word in raw for word in _ACTION_KEYWORDS):
        return LEVEL_ACTION
    return LEVEL_INFO


@dataclass
class NotifyItem:
    """归一化后的通知项：调用方可以传 NotifyLine、str 或 (level, text) 元组。"""

    level: str
    text: str
    task_id: int | None = None


def _as_item(raw) -> NotifyItem:
    if isinstance(raw, NotifyLine):
        return NotifyItem(raw.level, str(raw), raw.task_id)
    if isinstance(raw, (tuple, list)):
        parts = list(raw)
        # (level, text) 或 (level, text, task_id)；第一位不是合法级别时按纯内容兜底
        if parts and parts[0] in (LEVEL_ACTION, LEVEL_INFO):
            level = parts[0]
            text = str(parts[1]) if len(parts) > 1 else ""
            task_id = int(parts[2]) if len(parts) > 2 and parts[2] is not None else None
            return NotifyItem(level, text, task_id)
        text = "\n".join(str(p) for p in parts)
        return NotifyItem(classify(text), text)
    text = str(raw)
    return NotifyItem(classify(text), text)


# ---------------------------------------------------------------------------
# 免打扰窗口
# ---------------------------------------------------------------------------


def _parse_hm(value: str, fallback: str) -> int:
    """HH:MM → 当日分钟数；解析不了就用默认值（配置脏了不能把通知永久关死）。"""
    raw = (value or "").strip()
    try:
        hour, minute = raw.split(":")[:2]
        h, m = int(hour), int(minute)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except (ValueError, AttributeError):
        pass
    return _parse_hm(fallback, "00:00") if fallback != value else 0


def quiet_config(settings: dict | None = None) -> dict:
    """免打扰配置：开关 + 起止时间。取值优先 settings，缺失回落默认。"""
    raw = (settings or {}).get("notify_quiet")
    if not isinstance(raw, dict):
        raw = {}
    return {
        "enabled": bool(raw.get("enabled", DEFAULT_QUIET["enabled"])),
        "start": str(raw.get("start") or DEFAULT_QUIET["start"]),
        "end": str(raw.get("end") or DEFAULT_QUIET["end"]),
    }


def in_quiet(now: datetime | None = None, settings: dict | None = None) -> bool:
    """当前是否处于免打扰时段。跨天窗口（起 > 止）按"过夜"理解。"""
    cfg = quiet_config(settings)
    if not cfg["enabled"]:
        return False
    start = _parse_hm(cfg["start"], DEFAULT_QUIET["start"])
    end = _parse_hm(cfg["end"], DEFAULT_QUIET["end"])
    if start == end:
        return False  # 起止相同意味着"整天免打扰"，等于把通知关死，按关闭处理
    minutes = (now or datetime.now()).hour * 60 + (now or datetime.now()).minute
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


# ---------------------------------------------------------------------------
# 待发队列
# ---------------------------------------------------------------------------


def enqueue(level: str, body: str, title: str = "", task_id: int | None = None) -> int:
    """免打扰时段内攒一条。超出上限先丢最旧的，避免长期堆积。"""
    with session_scope() as session:
        rows = session.exec(select(NotifyPending).order_by(NotifyPending.id)).all()
        if len(rows) >= MAX_PENDING:
            for old in rows[: len(rows) - MAX_PENDING + 1]:
                session.delete(old)
        row = NotifyPending(
            level=level if level in (LEVEL_ACTION, LEVEL_INFO) else LEVEL_INFO,
            title=(title or "")[:200],
            body=body or "",
            task_id=task_id,
        )
        session.add(row)
        return int(row.id or 0)


def pending_rows() -> list[dict]:
    with session_scope() as session:
        rows = session.exec(select(NotifyPending).order_by(NotifyPending.id)).all()
    return [
        {
            "id": int(r.id or 0),
            "level": r.level,
            "title": r.title,
            "body": r.body,
            "task_id": r.task_id,
            "created_at": r.created_at.isoformat(timespec="seconds") if r.created_at else None,
        }
        for r in rows
    ]


def pending_count(level: str | None = None) -> int:
    rows = pending_rows()
    return len(rows) if level is None else sum(1 for r in rows if r["level"] == level)


def clear_pending(ids: list[int] | None = None) -> None:
    with session_scope() as session:
        rows = session.exec(select(NotifyPending)).all()
        for row in rows:
            if ids is None or int(row.id or 0) in set(ids):
                session.delete(row)


def _muted_task_ids() -> set[int]:
    """关掉「仅告知」的任务。需处理级不看这个开关——不能让静音漏掉真问题。"""
    try:
        with session_scope() as session:
            rows = session.exec(select(Task.id).where(Task.notify_info.is_(False))).all()
        return {int(i) for i in rows if i is not None}
    except Exception:  # noqa: BLE001 静音是优化，查不出来就当没静音
        return set()


# ---------------------------------------------------------------------------
# 摘要渲染与下发
# ---------------------------------------------------------------------------


def render_digest(rows: list[dict], settings: dict | None = None, now: datetime | None = None) -> tuple[str, str]:
    """把攒下的事件渲染成一条摘要。返回 (title, body)。

    需处理级逐条列出（用户要照着处理），仅告知级只给条数与前几条——
    半夜攒了 30 条转存成功，早上不该被一堵墙砸到。
    """
    cfg = quiet_config(settings)
    stamp = (now or datetime.now()).strftime("%m-%d %H:%M")
    action_rows = [r for r in rows if r["level"] == LEVEL_ACTION]
    info_rows = [r for r in rows if r["level"] == LEVEL_INFO]
    title = f"【免打扰摘要】{stamp} 共 {len(rows)} 条（需处理 {len(action_rows)}）"
    parts = [
        f"免打扰时段（{cfg['start']}–{cfg['end']}）内共 {len(rows)} 条通知，"
        f"其中需处理 {len(action_rows)} 条、仅告知 {len(info_rows)} 条。",
    ]
    if action_rows:
        parts.append("")
        parts.append(f"{LEVEL_ICON[LEVEL_ACTION]} 需处理：")
        parts.extend(f"• {r['body']}" for r in action_rows[:50])
        if len(action_rows) > 50:
            parts.append(f"… 另有 {len(action_rows) - 50} 条需处理事件")
    if info_rows:
        parts.append("")
        parts.append(f"{LEVEL_ICON[LEVEL_INFO]} 仅告知：")
        parts.extend(f"• {_first_line(r['body'])}" for r in info_rows[:5])
        if len(info_rows) > 5:
            parts.append(f"… 另有 {len(info_rows) - 5} 条告知类消息")
    return title, "\n".join(parts)


def _first_line(text: str) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""


async def send_now(title: str, content: str, push_config: dict, settings: dict, log) -> None:
    """默认下发器：尊重全局通知开关，逐渠道记录失败。

    push_all 的真实签名是 (title, content, push_config, log)——历史调用里多传了一个
    settings，会直接 TypeError 吞掉整条提醒（FR-02 的失效提醒一直没真正发出去）。
    """
    if not (settings or {}).get("notify_enabled", True):
        return
    from . import notify_service

    try:
        results = await notify_service.push_all(title, content, push_config, log=log)
    except Exception as exc:  # noqa: BLE001 通知失败不影响主流程
        if log:
            log("error", f"通知发送异常：{exc}")
        return
    for channel, ok, message in results or []:  # 渠道返回空/None 也算发过，别在这里炸
        if not ok and log:
            log("warn", f"通知渠道 {channel} 失败：{message}")


async def flush_pending(
    *,
    settings: dict,
    push_config: dict,
    log=None,
    send=send_now,
    force: bool = False,
    now: datetime | None = None,
) -> int:
    """补发免打扰期间攒下的事件，合成一条摘要。返回发出条数（0=没攒或仍在免打扰）。"""
    if not (settings or {}).get("notify_enabled", True):
        return 0
    rows = pending_rows()
    if not rows:
        return 0
    if not force and in_quiet(now, settings):
        return 0
    title, body = render_digest(rows, settings, now)
    await send(title, body, push_config, settings, log)
    clear_pending([r["id"] for r in rows])
    if log:
        log("info", f"免打扰摘要已补发：{len(rows)} 条")
    return len(rows)


async def dispatch(
    items,
    *,
    settings: dict,
    push_config: dict,
    log=None,
    send=send_now,
    title: str = "小盘通知",
    force: bool = False,
    now: datetime | None = None,
) -> dict:
    """分级下发：需处理与仅告知分开成两条，免打扰时段改为攒起来。

    返回 {"sent": 消息条数, "queued": 入队条数, "levels": {...}, "quiet": 是否免打扰}。
    """
    normalized = [_as_item(i) for i in (items or [])]
    normalized = [i for i in normalized if i.text.strip()]
    if not normalized:
        return {"sent": 0, "queued": 0, "quiet": False, "levels": {}}

    muted = _muted_task_ids()
    kept = [
        i
        for i in normalized
        if i.level == LEVEL_ACTION or i.task_id is None or i.task_id not in muted
    ]
    if not kept:
        return {"sent": 0, "queued": 0, "quiet": False, "levels": {}, "muted": len(normalized)}

    quiet = (not force) and in_quiet(now, settings)
    if quiet:
        for i in kept:
            enqueue(i.level, i.text, title=title, task_id=i.task_id)
        if log:
            log("info", f"免打扰时段，{len(kept)} 条通知已攒入次日摘要")
        return {
            "sent": 0,
            "queued": len(kept),
            "quiet": True,
            "levels": _count_by_level(kept),
        }

    # 出了免打扰先把攒下的补发，再发本次：否则摘要会被本次消息挤到后面，顺序看着像乱的
    await flush_pending(settings=settings, push_config=push_config, log=log, send=send, now=now)

    sent = 0
    for level in (LEVEL_ACTION, LEVEL_INFO):
        group = [i.text for i in kept if i.level == level]
        if not group:
            continue
        await send(
            f"【{LEVEL_LABEL[level]}】{title}",
            "\n".join(group),
            push_config,
            settings,
            log,
        )
        sent += 1
    return {"sent": sent, "queued": 0, "quiet": False, "levels": _count_by_level(kept), "muted": 0}


def _count_by_level(items: list[NotifyItem]) -> dict:
    return {
        LEVEL_ACTION: sum(1 for i in items if i.level == LEVEL_ACTION),
        LEVEL_INFO: sum(1 for i in items if i.level == LEVEL_INFO),
    }


__all__ = [
    "LEVEL_ACTION",
    "LEVEL_INFO",
    "LEVEL_LABEL",
    "NotifyLine",
    "classify",
    "dispatch",
    "enqueue",
    "flush_pending",
    "in_quiet",
    "line",
    "pending_count",
    "pending_rows",
    "quiet_config",
    "send_now",
]
