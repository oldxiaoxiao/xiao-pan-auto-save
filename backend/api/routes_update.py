"""版本检测与升级前备份（路线图「正式分发与更新」的第一刀）。

只告知、不代劳：是否下载新包、何时替换，由人决定。这里提供两件事——
把"有没有新版本"查清楚，以及升级前先把数据库快照一份。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .. import config

router = APIRouter(prefix="/api/update", tags=["update"])


@router.get("/check")
async def check(force: int = Query(default=0, ge=0, le=1)) -> dict:
    """比对本机版本与 GitHub 上的最新发布版。查不到时返回 reason，不抛异常。"""
    from ..services.update_service import check as _check

    return {"ok": True, "data": _check(force=bool(force))}


@router.post("/prebackup")
async def prebackup() -> dict:
    """升级前快照数据库：新版本若带数据库结构变更，至少有一条退路。"""
    from ..services.backup_service import backup_db

    backup_dir = config.DATA_DIR / "backups"
    try:
        path = backup_db(config.DB_PATH, backup_dir)
    except OSError as exc:
        raise HTTPException(500, f"升级前备份失败：{exc}") from exc
    if not path:
        raise HTTPException(400, "数据库不存在或为空，无需备份")
    return {
        "ok": True,
        "path": str(path),
        "size": path.stat().st_size,
        "message": f"已备份到 {path.name}（{path.stat().st_size} 字节）",
    }
