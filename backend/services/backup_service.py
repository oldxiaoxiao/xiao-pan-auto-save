"""数据库备份：把 data/xiao_pan.db 快照到 data/backups，保留最近 N 份。

用于防止误操作（如早期测试清空真实库）导致数据丢失。应用启动时自动快照一次，
也可用 scripts/backup_db.sh 挂 cron 定期备份。
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path


def backup_db(db_path: Path, backup_dir: Path, keep: int = 14) -> Path | None:
    """存在且非空才备份；按文件名倒序保留最近 keep 份，多余的删除。返回新备份路径或 None。"""
    if not (db_path.exists() and db_path.stat().st_size > 0):
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{db_path.stem}-{ts}.db"
    shutil.copy2(db_path, dest)
    snaps = sorted(backup_dir.glob(f"{db_path.stem}-*.db"), key=lambda p: p.name, reverse=True)
    for old in snaps[keep:]:
        old.unlink(missing_ok=True)
    return dest


BACKUP_KIND = "xiao-pan-backup"


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in str(value or "0").split(".")[:3]:
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def export_data(mode: str = "safe") -> dict:
    """结构化导出（FR-05）。

    mode=safe 时剔除账号 Cookie——直接拷 db 文件会把明文凭据一起带走，
    备份外发/存网盘就成了凭据泄露面。safe 是默认值。
    """
    from sqlmodel import select

    from .. import __version__
    from ..database import session_scope
    from ..models import Account, DownloadRecord, Setting, Task

    safe = mode != "full"
    with session_scope() as session:
        tasks = [t.model_dump(mode="json") for t in session.exec(select(Task)).all()]
        accounts = []
        for a in session.exec(select(Account)).all():
            row = a.model_dump(mode="json")
            if safe:
                row["cookie"] = ""
            accounts.append(row)
        settings = {s.key: s.get() for s in session.exec(select(Setting)).all()}
        downloads = [d.model_dump(mode="json") for d in session.exec(select(DownloadRecord)).all()]

    return {
        "meta": {
            "kind": BACKUP_KIND,
            "version": __version__,
            "exported_at": datetime.now(UTC).isoformat(),
            "mode": "safe" if safe else "full",
            "credentials_included": not safe,
            "counts": {
                "tasks": len(tasks),
                "accounts": len(accounts),
                "settings": len(settings),
                "downloads": len(downloads),
            },
        },
        "tasks": tasks,
        "accounts": accounts,
        "settings": settings,
        "downloads": downloads,
    }


def _rows_for(model, rows: list[dict]):
    """把导出的 JSON 行还原成模型实例。

    两处必须处理：
    1. 老备份可能缺字段 → 只取模型认识的键，缺的走默认值；
    2. 导出时 datetime 转成了 ISO 字符串，而 SQLModel 的 table=True 模型**不做字段校验**，
       字符串会原样传给 SQLite 并报 "only accepts Python datetime"——必须显式转回来。
    """
    from sqlalchemy import DateTime

    known = set(model.model_fields)
    date_cols = {c.name for c in model.__table__.columns if isinstance(c.type, DateTime)}
    out = []
    for row in rows:
        data = {}
        for key, value in row.items():
            if key not in known:
                continue
            if key in date_cols and isinstance(value, str) and value:
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    value = None
            data[key] = value
        out.append(model(**data))
    return out


def import_data(payload: dict, db_path: Path, backup_dir: Path) -> dict:
    """恢复备份（FR-05）。先自动快照当前库，再整库替换；版本更高一律拒绝。

    返回 {ok, message, restored, snapshot}。
    """
    from sqlmodel import select

    from .. import __version__
    from ..database import session_scope
    from ..models import Account, DownloadRecord, Setting, Task

    meta = payload.get("meta") or {}
    if meta.get("kind") != BACKUP_KIND:
        return {"ok": False, "message": "这不是本项目的备份文件（缺少标识）"}
    incoming = _version_tuple(meta.get("version", "0"))
    current = _version_tuple(__version__)
    if incoming > current:
        return {
            "ok": False,
            "message": f"备份来自更高版本（{meta.get('version')}），请先升级程序再恢复",
        }

    # 恢复是不可逆操作：动手前先给当前库留一份快照
    snapshot = backup_db(db_path, backup_dir)

    tasks = _rows_for(Task, payload.get("tasks") or [])
    accounts = _rows_for(Account, payload.get("accounts") or [])
    downloads = _rows_for(DownloadRecord, payload.get("downloads") or [])

    try:
        with session_scope() as session:
            for model in (DownloadRecord, Task, Setting, Account):
                for row in session.exec(select(model)).all():
                    session.delete(row)
            session.flush()
            for row in accounts:
                session.add(row)
            for row in tasks:
                session.add(row)
            for row in downloads:
                session.add(row)
            for key, value in (payload.get("settings") or {}).items():
                session.add(Setting(key=key, value=json.dumps(value, ensure_ascii=False)))
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "message": f"恢复失败，数据未改动：{exc}",
            "snapshot": str(snapshot) if snapshot else None,
        }

    return {
        "ok": True,
        "message": "恢复完成",
        "restored": {
            "tasks": len(tasks),
            "accounts": len(accounts),
            "downloads": len(downloads),
            "settings": len(payload.get("settings") or {}),
        },
        "credentials_included": bool(meta.get("credentials_included")),
        "snapshot": str(snapshot) if snapshot else None,
    }
