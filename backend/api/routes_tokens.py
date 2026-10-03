"""对外 API Token 管理（WebUI 设置页使用）。"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from ..database import session_scope
from ..models import ExternalApiToken

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


class TokenIn(BaseModel):
    name: str = "油猴脚本"


@router.get("")
async def list_tokens() -> list[dict]:
    with session_scope() as session:
        rows = session.exec(select(ExternalApiToken).order_by(ExternalApiToken.created_at)).all()
        return [
            {
                "token_preview": f"{r.token[:6]}…{r.token[-4:]}",
                "name": r.name,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.post("")
async def create_token(body: TokenIn) -> dict:
    token = secrets.token_hex(16)
    with session_scope() as session:
        session.add(ExternalApiToken(token=token, name=body.name.strip() or "未命名"))
    return {"token": token, "name": body.name}  # 完整 token 仅此一次返回


class TokenDelIn(BaseModel):
    token: str


@router.delete("")
async def delete_token(body: TokenDelIn) -> dict:
    with session_scope() as session:
        row = session.get(ExternalApiToken, body.token)
        if row is None:
            raise HTTPException(404, "token 不存在")
        session.delete(row)
    return {"ok": True}
