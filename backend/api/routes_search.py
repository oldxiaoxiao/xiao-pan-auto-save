"""智能搜索建议 + 分享链接有效性验证。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..api.deps import get_setting, set_setting
from ..services.search_service import search_all

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/suggestions")
async def suggestions(q: str, d: str = "0") -> dict:
    if not q.strip():
        return {"ok": True, "data": []}
    source_cfg = get_setting("source") or {}
    result = await search_all(q.strip(), d == "1", source_cfg)
    if result["new_cs_token"]:
        cfg = dict(source_cfg)
        cs = dict(cfg.get("cloudsaver") or {})
        cs["token"] = result["new_cs_token"]
        cfg["cloudsaver"] = cs
        set_setting("source", cfg)
    return {"ok": True, "data": result["data"]}


class ValidateIn(BaseModel):
    shareurl: str


@router.post("/validate")
async def validate(body: ValidateIn) -> dict:
    """换取 stoken 验证链接有效性（点选建任务前调用）。"""
    from ..core.router import route_driver
    from ..drivers.base import ShareBanned, ShareUnavailable

    cls = route_driver(body.shareurl)
    if cls is None:
        return {"ok": False, "message": "无法识别的网盘链接"}
    if not cls.supported:
        return {"ok": False, "message": f"{cls.name} 驱动即将支持", "pending": True}
    from ..api.routes_files import _driver

    drv = _driver(cls.key)
    try:
        ref = drv.parse_share(body.shareurl)
        items = await drv.list_share(ref, "")
        return {"ok": True, "count": len(items)}
    except ShareBanned as exc:
        return {"ok": False, "message": exc.message}
    except ShareUnavailable as exc:
        return {"ok": False, "message": f"网络异常：{exc.message}", "retry": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": str(exc)}
    finally:
        await drv.close()
