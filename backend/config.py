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

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
