"""数据库备份：把 data/xiao_pan.db 快照到 data/backups，保留最近 N 份。

用于防止误操作（如早期测试清空真实库）导致数据丢失。应用启动时自动快照一次，
也可用 scripts/backup_db.sh 挂 cron 定期备份。
"""

from __future__ import annotations

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
