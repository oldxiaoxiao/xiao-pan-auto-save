"""本机文件系统接口：目录浏览与"在文件管理器中打开"。

给设置页挑下载目录用，免去手敲绝对路径。路径是**运行后端那台机器**上的路径：
桌面客户端即用户本机，Compose 即容器内——与下载根目录一贯的语境一致。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from ..services.local_fs import FsError

router = APIRouter(prefix="/api/fs", tags=["fs"])


class RevealIn(BaseModel):
    path: str


@router.get("/roots")
async def get_roots() -> dict:
    from ..services.local_fs import roots

    return {"ok": True, "data": {"roots": roots(), "default": _default()}}


@router.get("/dirs")
async def get_dirs(path: str = Query(default="")) -> dict:
    from ..services.local_fs import list_dir

    try:
        return {"ok": True, "data": list_dir(path)}
    except FsError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/reveal")
async def post_reveal(body: RevealIn) -> dict:
    from ..services.local_fs import reveal

    try:
        return reveal(body.path)
    except FsError as exc:
        raise HTTPException(400, str(exc)) from exc


def _default() -> str:
    from ..services.local_fs import default_path

    return default_path()
