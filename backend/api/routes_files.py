"""文件浏览 API：网盘目录列表/重命名/删除、分享预览（含正则效果列）。"""

from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from ..config import PROXY
from ..core.magic import MagicRename
from ..database import session_scope
from ..drivers import get_driver_class
from ..drivers.base import DriveError, FsItem, ShareBanned, ShareUnavailable
from ..models import Account

router = APIRouter(prefix="/api/files", tags=["files"])


def _primary_account(driver_key: str) -> Account:
    with session_scope() as session:
        accounts = session.exec(
            select(Account)
            .where(Account.enabled, Account.driver_key == driver_key)
            .order_by(Account.sort_order, Account.id)
        ).all()
        acc = next((a for a in accounts if a.can_save), accounts[0] if accounts else None)
    if not acc:
        raise HTTPException(400, f"未配置可用的 {driver_key} 账号")
    return acc


def _driver(driver_key: str):
    acc = _primary_account(driver_key)
    cls = get_driver_class(driver_key)
    if cls is None or not cls.supported:
        raise HTTPException(400, f"{driver_key} 驱动即将支持")
    return cls(cookie=acc.cookie, proxy=PROXY, index=acc.sort_order)


def _item_dict(i: FsItem, **extra) -> dict:
    return {
        "fid": i.fid,
        "name": i.name,
        "is_dir": i.is_dir,
        "size": i.size,
        "mtime": i.mtime,
        "token": i.token,
        **extra,
    }


# 网盘返回里代表"未登录/Cookie 失效"的常见关键字（各驱动通用）。
_LOGIN_HINTS = ("require login", "unauthorized", "not login", "未登录", "登录", "cookie", "token", "31001")


def _drive_http(action: str, exc: DriveError) -> HTTPException:
    """把驱动异常翻译成前端可读的友好提示，避免裸 500。"""
    msg = (exc.message or str(exc)).strip()
    low = msg.lower()
    if any(k in low for k in _LOGIN_HINTS):
        return HTTPException(502, "账号 Cookie 可能已失效，请到「账号」页更新 Cookie 后重试")
    return HTTPException(502, f"{action}失败：{msg}")


class RenameIn(BaseModel):
    path: str
    new_name: str
    driver: str = "quark"


class DeleteIn(BaseModel):
    path: str
    driver: str = "quark"
    purge: bool = False


@router.get("/dir")
async def list_dir(path: str = "/", driver: str = "quark") -> dict:
    drv = _driver(driver)
    try:
        norm = re.sub(r"/{2,}", "/", f"/{path.strip('/')}")
        items = await drv.list_dir(norm)
        parent = "/" if norm == "/" else norm.rsplit("/", 1)[0] or "/"
        return {
            "path": norm,
            "parent": parent,
            "at_root": norm == "/",
            "list": [_item_dict(i) for i in items],
        }
    except DriveError as exc:
        raise _drive_http("读取目录", exc) from exc
    finally:
        await drv.close()


@router.post("/rename")
async def rename(body: RenameIn) -> dict:
    drv = _driver(body.driver)
    try:
        parent = body.path.rsplit("/", 1)[0] or "/"
        name = body.path.rsplit("/", 1)[1]
        items = await drv.list_dir(parent)
        target = next((i for i in items if i.name == name), None)
        if target is None:
            raise HTTPException(404, f"未找到文件: {body.path}")
        await drv.rename(target, body.new_name)
        return {"ok": True}
    except DriveError as exc:
        raise _drive_http("重命名", exc) from exc
    finally:
        await drv.close()


@router.post("/delete")
async def delete(body: DeleteIn) -> dict:
    drv = _driver(body.driver)
    try:
        if not drv.has("delete"):
            raise HTTPException(400, "驱动不支持删除")
        parent = body.path.rsplit("/", 1)[0] or "/"
        name = body.path.rsplit("/", 1)[1]
        items = await drv.list_dir(parent)
        target = next((i for i in items if i.name == name), None)
        if target is None:
            raise HTTPException(404, f"未找到文件: {body.path}")
        await drv.delete_items([target], purge=body.purge)
        return {"ok": True}
    except DriveError as exc:
        raise _drive_http("删除", exc) from exc
    finally:
        await drv.close()


class SharePreviewIn(BaseModel):
    shareurl: str
    path: str = ""  # 分享内子路径
    taskname: str = ""
    pattern: str = ""
    replace: str = ""
    ignore_extension: bool = False
    update_subdir: str = ""
    savepath: str = ""  # 提供时给出"已存在"标记列


@router.post("/share/preview")
async def share_preview(body: SharePreviewIn) -> dict:
    from ..core.router import route_driver

    cls = route_driver(body.shareurl)
    if cls is None or not cls.supported:
        raise HTTPException(400, "该链接没有已支持的网盘驱动")
    drv = _driver(cls.key)
    try:
        try:
            ref = drv.parse_share(body.shareurl)
            items = await drv.list_share(ref, body.path)
        except ShareBanned as exc:
            return {"ok": False, "banned": True, "message": exc.message, "list": []}
        except ShareUnavailable as exc:
            return {"ok": False, "banned": False, "message": exc.message, "list": []}
        except DriveError as exc:
            raise HTTPException(502, str(exc)) from exc

        # 正则处理效果预览（对齐原项目 fileSelect 的 file_name_re/file_name_saved）
        dir_names: list[str] = []
        if body.savepath:
            try:
                dir_names = [i.name for i in await drv.list_dir(body.savepath)]
            except DriveError:
                dir_names = []

        mr = MagicRename()
        mr.set_taskname(body.taskname)
        pattern, replace = mr.magic_regex_conv(body.pattern, body.replace)
        out = []
        for i in items:
            row = _item_dict(i)
            search_pattern = body.update_subdir if (i.is_dir and body.update_subdir) else pattern
            if re.search(search_pattern or "", i.name):
                if i.is_dir:
                    row["name_re"] = i.name
                else:
                    row["name_re"] = mr.sub(pattern, replace, i.name)
                saved = mr.is_exists(row["name_re"], dir_names, body.ignore_extension and not i.is_dir)
                if saved:
                    row["saved_as"] = saved
            out.append(row)
        if re.search(r"\{I+\}", replace or ""):
            view = [{"name_re": r.get("name_re"), "mtime": r["mtime"], "is_dir": r["is_dir"]} for r in out]
            mr.sort_file_list(view, dir_filename_dict={})
            for r, v in zip(out, view, strict=True):
                if "name_re" in r:
                    r["name_re"] = v["name_re"]

        return {"ok": True, "path": body.path, "sub_names": ref.sub_names, "list": out}
    finally:
        await drv.close()
