"""数据库备份：把 data/xiao_pan.db 快照到 data/backups，保留最近 N 份。

用于防止误操作（如早期测试清空真实库）导致数据丢失。应用启动时自动快照一次，
也可用 scripts/backup_db.sh 挂 cron 定期备份。

FR-11：备份走**一致性快照**，不再直接复制 db 文件。WAL 模式下主库旁边还有一个
`-wal` 文件，尚未 checkpoint 的提交只在里面——下载高峰期连写下，复制主库会得到一份
"少了最后几十条"的库，而且**打开时不报错**，等你真要恢复才发现。所以：

1. 首选 `VACUUM INTO`：SQLite 自己产出完整、一致、且顺带整理过的新库；
2. 退而求其次用官方 online backup API（同样是事务一致的快照）；
3. 两者都不成（比如目标文件不是 SQLite 库）才复制文件，并留一条 warn——
   保底拿到点东西比什么都拿不到强，但要说清楚它不可信。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _snapshot_vacuum(db_path: Path, dest: Path) -> bool:
    """VACUUM INTO 产出一致性快照。目标文件必须不存在。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(dest),))
        return True
    finally:
        conn.close()


def _snapshot_backup_api(db_path: Path, dest: Path) -> bool:
    """sqlite3 官方 online backup API：事务一致的快照，不要求目标不存在。"""
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            return True
        finally:
            dst.close()
    finally:
        src.close()


def _snapshot_consistent(db_path: Path, dest: Path) -> bool:
    """一致性快照优先；失败逐级降级。返回是否拿到了可信快照。"""
    for step in (_snapshot_vacuum, _snapshot_backup_api):
        try:
            if step(db_path, dest):
                return True
        except (sqlite3.Error, OSError):
            dest.unlink(missing_ok=True)  # 别留下半截文件冒充备份
            continue
    return False


def backup_db(db_path: Path, backup_dir: Path, keep: int = 14) -> Path | None:
    """存在且非空才备份；按文件名倒序保留最近 keep 份，多余的删除。返回新备份路径或 None。"""
    if not (db_path.exists() and db_path.stat().st_size > 0):
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = backup_dir / f"{db_path.stem}-{ts}.db"

    if not _snapshot_consistent(db_path, dest):
        # 降级到文件复制：至少留个东西，但它可能缺最后几次提交
        shutil.copy2(db_path, dest)
        _warn(f"一致性快照失败，{dest.name} 为文件复制，可能缺最近的提交")

    snaps = sorted(backup_dir.glob(f"{db_path.stem}-*.db"), key=lambda p: p.name, reverse=True)
    for old in snaps[keep:]:
        old.unlink(missing_ok=True)
    return dest


def _warn(message: str) -> None:
    try:
        from ..core.logstream import hub

        hub.make_logger("backup")("warn", message)
    except Exception:  # noqa: BLE001 日志不可用时不该让备份失败
        pass


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
