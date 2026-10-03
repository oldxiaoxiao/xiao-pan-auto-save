#!/usr/bin/env bash
# 定期备份数据库快照，可挂 cron。例：
#   0 3 * * * /path/to/scripts/backup_db.sh
# 环境变量：
#   KEEP      保留最近几份（默认 14）
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${REPO_DIR}/.venv/bin/python"
[ -x "$PY" ] || PY="python3"

cd "$REPO_DIR"
KEEP="${KEEP:-14}" "$PY" - <<'PY'
import os

from backend.config import DATA_DIR, DB_PATH
from backend.services.backup_service import backup_db

dest = backup_db(DB_PATH, DATA_DIR / "backups", keep=int(os.environ.get("KEEP", "14")))
print(f"已备份 {DB_PATH} -> {dest}" if dest else f"跳过备份：{DB_PATH} 不存在或为空")
PY
