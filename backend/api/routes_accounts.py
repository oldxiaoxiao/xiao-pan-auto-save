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
    """掩码只用于展示：解密不出来时不能返回可还原的片段，直接给不可推断的占位。"""
    if not cookie:
        return ""
    if len(cookie) <= 12:
        return "***"
    return f"{cookie[:6]}...{cookie[-6:]}"


def _to_out(acc: Account) -> AccountOut:
    from ..services.credential_store import plain_cookie

    data = acc.model_dump(exclude={"cookie"})
    # 解不开（密钥丢失）时明确给"需重新录入"，而不是拿密文切一段出来冒充掩码
    plain = plain_cookie(acc)
    data["cookie_masked"] = "*** 凭据无法解密，请重新录入 ***" if plain is None else _mask(plain)
    data.pop("cookie_enc", None)  # 密文绝不出现在响应里
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
    from ..services.credential_store import store_cookie

    with session_scope() as session:
        data = body.model_dump()
        cookie_value = data.pop("cookie", "")
        acc = Account(**data)
        store_cookie(acc, cookie_value)  # FR-08：写库即加密，明文不落盘
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
        cookie_changed = bool(data["cookie"].strip())
        data.pop("cookie")  # Cookie 单独走加密写入，绝不走普通字段赋值
        for k, v in data.items():
            setattr(acc, k, v)
        if cookie_changed:
            from ..services.credential_store import store_cookie

            store_cookie(acc, body.cookie)
        if cookie_changed:
            # FR-02：换了 Cookie 就清掉失效标记，否则徽标与"任务暂停"会一直挂着
            acc.check_ok = True
            acc.check_message = ""
            acc.invalid_notified_at = None
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
