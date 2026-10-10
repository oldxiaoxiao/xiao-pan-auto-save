"""导入旧版 quark-auto-save 的 quark_config.json（一次性迁移）。

映射：cookie(str|list)→夸克账号；tasklist→任务（update_subdir_resave_mode→
update_subdir_resave；插件 addition 不支持，导入结果中列明）；
crontab/push_config/magic_regex/source→全局设置。
"""

from __future__ import annotations

import json

from sqlmodel import delete, select

from ..database import session_scope
from ..models import Account, Setting, Task

_TASK_KEYS_HINT = "旧版字段直映射；update_subdir_resave_mode→update_subdir_resave；addition(插件) 丢弃"


def analyze(config: dict) -> dict:
    """预览迁移内容，不落库。"""
    _validate(config)
    cookies = config.get("cookie") or []
    if isinstance(cookies, str):
        cookies = [cookies] if cookies.strip() else []
    tasks = config.get("tasklist") or []
    with_plugin = [t.get("taskname", "?") for t in tasks if t.get("addition")]
    return {
        "accounts": sum(1 for c in cookies if str(c).strip()),
        "tasks": len(tasks),
        "settings": [k for k in ("crontab", "push_config", "magic_regex", "source") if k in config],
        "plugin_tasks_ignored": with_plugin,
    }


def _validate(config: dict) -> None:
    if "tasklist" not in config and "cookie" not in config:
        raise ValueError("不像 quark_config.json：缺少 tasklist/cookie 字段")
    cookies = config.get("cookie") or []
    if not isinstance(cookies, (str, list)) or isinstance(cookies, list) and any(not isinstance(c, str) for c in cookies):
        raise ValueError("cookie 必须是字符串或字符串列表")
    tasks = config.get("tasklist") or []
    if not isinstance(tasks, list) or any(not isinstance(t, dict) for t in tasks):
        raise ValueError("tasklist 必须是任务对象列表")
    for task in tasks:
        for key in ("taskname", "shareurl", "savepath"):
            if task.get(key) is not None and not isinstance(task[key], str):
                raise ValueError(f"任务 {key} 必须是字符串")
        addition = task.get("addition") or {}
        if not isinstance(addition, dict) or not isinstance(addition.get("aria2") or {}, dict):
            raise ValueError("任务 addition/aria2 必须是对象")
    plugins = config.get("plugins") or {}
    if not isinstance(plugins, dict) or not isinstance(plugins.get("aria2") or {}, dict):
        raise ValueError("plugins/aria2 必须是对象")


def import_config(config: dict, overwrite: bool = False) -> dict:
    """执行迁移。overwrite=True 时先清空现有夸克账号与全部任务。"""
    preview = analyze(config)
    if (preview["accounts"] or preview["tasks"]) and not overwrite:
        with session_scope() as session:
            exists = (
                session.exec(select(Account.id).limit(1)).first()
                or session.exec(select(Task.id).limit(1)).first()
            )
        if exists:
            return {"ok": False, "message": "已存在账号/任务数据，请使用覆盖导入", **preview}

    with session_scope() as session:
        if overwrite:
            session.exec(delete(Task))
            session.exec(delete(Account).where(Account.driver_key == "quark"))

        def write_setting(key: str, value) -> None:
            row = session.get(Setting, key)
            if row is None:
                row = Setting(key=key)
                session.add(row)
            row.set(value)

        cookies = config.get("cookie") or []
        if isinstance(cookies, str):
            cookies = [cookies] if cookies.strip() else []
        account_ids: list[int] = []
        from .credential_store import store_cookie

        for i, ck in enumerate(c for c in cookies if str(c).strip()):
            acc = Account(
                driver_key="quark",
                name=f"迁移账号{i + 1}",
                cookie="",
                enabled=True,
                sort_order=i,
            )
            store_cookie(acc, str(ck).strip())  # FR-08：导入的 Cookie 同样加密入库
            session.add(acc)
            session.flush()
            account_ids.append(acc.id)

        n_tasks = 0
        for order, t in enumerate(config.get("tasklist") or []):
            if not (t.get("taskname") and t.get("shareurl") and t.get("savepath")):
                continue
            aria2 = (t.get("addition") or {}).get("aria2") or {}
            row = Task(
                taskname=t["taskname"],
                shareurl=t["shareurl"],
                savepath=t["savepath"],
                pattern=t.get("pattern") or "",
                replace=t.get("replace") or "",
                startfid=t.get("startfid") or "",
                update_subdir=t.get("update_subdir") or "",
                enddate=str(t.get("enddate") or ""),
                shareurl_ban=t.get("shareurl_ban") or "",
                ignore_extension=bool(t.get("ignore_extension")),
                update_subdir_resave=bool(t.get("update_subdir_resave_mode")),
                auto_download=bool(aria2.get("auto_download")),
                download_subdir=bool(aria2.get("download_subdir")),
                download_savepath=str(aria2.get("save_path") or ""),
                runweek=json.dumps(t.get("runweek") or []),
                disabled=bool(t.get("disabled")),
                sort_order=order,
                account_id=account_ids[0] if account_ids else None,
            )
            session.add(row)
            n_tasks += 1

        for key in ("crontab", "push_config", "magic_regex", "source"):
            if key in config and config[key] is not None:
                write_setting(key, config[key])

        # 旧 aria2 插件全局配置 → 下载设置
        old_aria2 = (config.get("plugins") or {}).get("aria2") or {}
        if old_aria2:
            write_setting(
                "download",
                {
                    "mode": "aria2",
                    "dir": old_aria2.get("dir") or "",
                    "concurrency": 2,
                    "aria2": {
                        "host_port": old_aria2.get("host_port") or "",
                        "secret": old_aria2.get("secret") or "",
                        "pause": bool(old_aria2.get("pause")),
                    },
                    "emby": {},
                },
            )

    return {"ok": True, "imported_accounts": len(account_ids), "imported_tasks": n_tasks, **preview}
