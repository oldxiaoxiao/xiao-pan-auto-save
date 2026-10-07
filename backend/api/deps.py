"""API 依赖：全局设置读写。"""

from __future__ import annotations

import json

from sqlmodel import select

from ..config import CRONTAB_DEFAULT
from ..database import session_scope
from ..models import Setting

DEFAULT_SETTINGS: dict[str, object] = {
    "crontab": CRONTAB_DEFAULT,
    "push_config": {"CONSOLE": True},
    "magic_regex": {},
    "source": {
        "engines": [
            {"id": "kkso-default", "type": "kkso", "name": "夸克搜", "server": "https://kkso.net", "enable": True}
        ]
    },
    "notify_enabled": True,
    "sign_enabled": True,
    "task_defaults": {
        "savepath_root": "/来自：分享",
        "auto_download": True,
        "run_mode": "follow",
        "pattern": "",
        "quality": "",
        "subdir_filter": True,
    },
    "download": {
        "mode": "builtin",
        "dir": "",
        "concurrency": 2,
        "history_retention": "days_90",
        "aria2": {"host_port": "", "secret": "", "pause": False},
        "emby": {"url": "", "token": ""},
    },
}


def get_setting(key: str):
    with session_scope() as session:
        row = session.get(Setting, key)
        if row is None:
            return DEFAULT_SETTINGS.get(key)
        return row.get()


def set_setting(key: str, value) -> None:
    with session_scope() as session:
        row = session.get(Setting, key)
        if row is None:
            row = Setting(key=key)
            session.add(row)
        row.set(value)


def all_settings() -> dict[str, object]:
    merged = dict(DEFAULT_SETTINGS)
    with session_scope() as session:
        for row in session.exec(select(Setting)):
            merged[row.key] = row.get()
    return {k: v for k, v in merged.items() if not k.startswith("_")}


def _loads(value: str, default=None):
    try:
        return json.loads(value) if value else default
    except json.JSONDecodeError:
        return default
