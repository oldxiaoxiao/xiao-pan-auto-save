"""FR-08 凭据本地加密：明文不落盘、迁移不中断、密钥丢失要明确提示。"""

from __future__ import annotations

import pytest
from sqlmodel import delete

from backend.database import session_scope
from backend.models import Account
from backend.services import ai_secret, credential_store


@pytest.fixture(autouse=True)
def clean_db():
    with session_scope() as s:
        s.exec(delete(Account))
    yield
    with session_scope() as s:
        s.exec(delete(Account))


def _acc(cookie: str = "__uid=12345; secret=abc") -> Account:
    with session_scope() as s:
        acc = Account(driver_key="quark", cookie="", name="主号", enabled=True)
        s.add(acc)
        s.commit()
        s.refresh(acc)
        credential_store.store_cookie(acc, cookie)
        s.add(acc)
        s.commit()
        s.refresh(acc)
        return acc


def test_cookie_is_encrypted_at_rest():
    acc = _acc()
    assert acc.cookie == "", "明文字段必须清空"
    assert acc.cookie_enc, "必须写入密文"
    assert "__uid=12345" not in acc.cookie_enc
    with session_scope() as s:
        row = s.get(Account, acc.id)
        assert "secret=abc" not in row.cookie_enc


def test_plain_cookie_roundtrip():
    acc = _acc("CK-VALUE-123")
    with session_scope() as s:
        row = s.get(Account, acc.id)
        assert credential_store.plain_cookie(row) == "CK-VALUE-123"


def test_migration_keeps_value_and_clears_plaintext():
    """升级路径不得中断已有任务：迁移前后取到的 Cookie 完全一致。"""
    with session_scope() as s:
        acc = Account(driver_key="quark", cookie="OLD-PLAIN-CK", name="旧号", enabled=True)
        s.add(acc)
        s.commit()
        s.refresh(acc)
        acc_id = acc.id

    moved = credential_store.migrate_plaintext_cookies()
    assert moved == 1

    with session_scope() as s:
        row = s.get(Account, acc_id)
        assert row.cookie == "", "迁移后明文必须清空"
        assert row.cookie_enc
        assert credential_store.plain_cookie(row) == "OLD-PLAIN-CK", "取值不能变，否则已有任务会断"


def test_migration_is_idempotent():
    _acc()
    assert credential_store.migrate_plaintext_cookies() == 0  # 已是密文，无需再动


def test_lost_key_returns_none_not_empty():
    """密钥丢失必须返回 None（明确"解不开"），不能退化成空串——

    空串会让驱动照常发请求，用户只看到"任务跑不动"，根本想不到是密钥丢了。
    """
    acc = _acc()

    def boom(_token: str) -> str:
        raise ai_secret.AiSecretError("密钥文件丢失")

    import backend.services.credential_store as cs

    original = cs.decrypt
    cs.decrypt = boom
    try:
        with session_scope() as s:
            row = s.get(Account, acc.id)
            assert cs.plain_cookie(row) is None
    finally:
        cs.decrypt = original


def test_no_cookie_returns_empty_string():
    with session_scope() as s:
        acc = Account(driver_key="quark", cookie="", name="空号", enabled=True)
        s.add(acc)
        s.commit()
        s.refresh(acc)
        assert credential_store.plain_cookie(acc) == ""


def test_api_never_exposes_ciphertext():
    """响应里不能出现密文：它虽然解不开，但属于凭据材料，不该进入日志与抓包。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    _acc()
    with TestClient(app) as client:
        resp = client.get("/api/accounts")
        assert resp.status_code == 200
        body = resp.text
        assert "cookie_enc" not in body
        assert "__uid=12345" not in body
        assert "secret=abc" not in body
        row = resp.json()[0]
        assert row["cookie_masked"] and row["cookie_masked"] != "__uid=12345; secret=abc"
