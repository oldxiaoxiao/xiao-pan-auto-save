"""全局配置：环境变量 + 数据目录。"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "xiao_pan.db"

WEBUI_USERNAME = os.getenv("WEBUI_USERNAME", "admin")
WEBUI_PASSWORD = os.getenv("WEBUI_PASSWORD", "")

CRONTAB_DEFAULT = os.getenv("CRONTAB", "0 9 * * *")
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "30"))
PROXY = os.getenv("PROXY", "")

# 置 1 时应用启动不跑下载历史清理（pytest 隔离用：启动清理会删共享临时库里其它用例
# seed 的过期终态行）。生产默认不跳过——启动补一次清理是设计行为。
SKIP_STARTUP_PRUNE = os.getenv("XIAO_PAN_SKIP_STARTUP_PRUNE", "") == "1"

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
