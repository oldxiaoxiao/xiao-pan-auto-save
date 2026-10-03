"""账号 CRUD、健康检查与签到。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlmodel import select

from ..database import session_scope
from ..drivers import DRIVERS
from ..models import Account
from ..schemas import AccountIn, AccountOut
from ..services import account_service

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


def _mask(cookie: str) -> str:
    if len(cookie) <= 12:
        return "***"
    return f"{cookie[:6]}...{cookie[-6:]}"


def _to_out(acc: Account) -> AccountOut:
    data = acc.model_dump(exclude={"cookie"})
    data["cookie_masked"] = _mask(acc.cookie)
    for key in ("last_check_at", "last_sign_at"):
        val = getattr(acc, key)
        data[key] = val.isoformat() if val else None
    return AccountOut(**data)


@router.get("", response_model=list[AccountOut])
async def list_accounts() -> list[AccountOut]:
    with session_scope() as session:
        accounts = session.exec(select(Account).order_by(Account.sort_order, Account.id)).all()
        return [_to_out(a) for a in accounts]


@router.post("", response_model=AccountOut)
async def create_account(body: AccountIn) -> AccountOut:
    if body.driver_key not in DRIVERS:
        raise HTTPException(400, f"未知驱动: {body.driver_key}")
    if not body.cookie.strip():
        raise HTTPException(400, "创建账号必须提供 Cookie")
    with session_scope() as session:
        acc = Account(**body.model_dump())
        session.add(acc)
        session.commit()
        session.refresh(acc)
        return _to_out(acc)


@router.put("/{account_id}", response_model=AccountOut)
async def update_account(account_id: int, body: AccountIn) -> AccountOut:
    with session_scope() as session:
        acc = session.get(Account, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        data = body.model_dump()
        if not data["cookie"].strip():
            data.pop("cookie")  # 留空 = 保持原 Cookie（前端只有掩码）
        for k, v in data.items():
            setattr(acc, k, v)
        session.add(acc)
        session.commit()
        session.refresh(acc)
        return _to_out(acc)


@router.delete("/{account_id}")
async def delete_account(account_id: int) -> dict:
    with session_scope() as session:
        acc = session.get(Account, account_id)
        if not acc:
            raise HTTPException(404, "账号不存在")
        session.delete(acc)
    return {"ok": True}


@router.post("/refresh")
async def refresh() -> list[dict]:
    return await account_service.refresh_accounts()


@router.post("/sign")
async def sign() -> list[dict]:
    return await account_service.sign_accounts()
