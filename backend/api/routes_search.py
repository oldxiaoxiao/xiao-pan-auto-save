"""智能搜索建议 + 分享链接有效性验证。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..api.deps import get_setting, set_setting
from ..services.search_engines import engine_type_specs, normalize_source_cfg
from ..services.search_service import search_all

router = APIRouter(prefix="/api/search", tags=["search"])


@router.get("/engine-types")
async def engine_types() -> dict:
    """有哪些协议可配、各要填哪些字段：前端按这个渲染引擎表单，加协议不用改 UI。"""
    return {"ok": True, "data": engine_type_specs()}


@router.get("/suggestions")
async def suggestions(q: str, d: str = "0", engine: str = "") -> dict:
    """engine 传引擎 id；留空=搜所有启用引擎。errors 逐源说明谁没出结果。"""
    if not q.strip():
        return {"ok": True, "data": [], "errors": []}
    engines = normalize_source_cfg(get_setting("source"))
    result = await search_all(q.strip(), d == "1", {"engines": engines}, engine_id=engine)
    written = False
    for engine_id, token in result["token_updates"].items():
        hit = next((item for item in engines if item["id"] == engine_id), None)
        if hit is not None and hit.get("token") != token:
            hit["token"] = token
            written = True
    if written:
        set_setting("source", {"engines": engines})
    return {"ok": True, "data": result["data"], "errors": result["errors"]}


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
