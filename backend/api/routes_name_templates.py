"""FR-10 命名模板库接口：内置/个人模板、套用前预览。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import name_template_service as svc

router = APIRouter(prefix="/api/name-templates", tags=["name-templates"])


class TemplateIn(BaseModel):
    name: str
    pattern: str = ""
    replace: str = ""
    desc: str = ""


class PreviewIn(BaseModel):
    pattern: str = ""
    replace: str = ""
    samples: list[str] = []
    taskname: str = ""


@router.get("")
async def list_templates() -> dict:
    return {"ok": True, "data": {"builtin": svc.builtin(), "custom": svc.custom()}}


@router.post("/preview")
async def preview(body: PreviewIn) -> dict:
    """套用前先看效果：每个样本重命名成什么、正则有没有命中。"""
    return {
        "ok": True,
        "data": svc.preview(body.pattern, body.replace, body.samples, body.taskname),
    }


@router.post("")
async def save_template(body: TemplateIn) -> dict:
    if not body.name.strip():
        raise HTTPException(400, "模板名不能为空")
    if not body.pattern.strip() and not body.replace.strip():
        raise HTTPException(400, "正则与替换式至少要填一个")
    return {"ok": True, "data": svc.save_custom(body.name.strip(), body.pattern, body.replace, body.desc)}


@router.delete("/{template_id}")
async def remove_template(template_id: str) -> dict:
    return {"ok": True, "data": svc.delete_custom(template_id)}
