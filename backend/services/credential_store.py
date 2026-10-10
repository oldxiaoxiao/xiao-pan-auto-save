"""网盘账号凭据的本地加密存储（FR-08）。

与 AI Key 共用同一套本地密钥（``ai_secret``）：同一台机器、同一个数据目录，
维护两套密钥只会多一个"丢了就解不开"的面。

三件事：
1. 写库即加密，明文不落盘；
2. 取用时才解密注入驱动，用完即弃（不缓存到模块级变量）；
3. 密钥丢了要**明确说"请重新录入"**，不能静默当成空 Cookie——
   那会让所有任务失败，而用户看到的只是"任务跑不动"，根本想不到是密钥丢了。
"""

from __future__ import annotations

from sqlmodel import select

from ..database import session_scope
from ..models import Account
from .ai_secret import AiSecretError, decrypt, encrypt


class CredentialError(RuntimeError):
    """凭据无法解密（密钥文件丢失或被替换）。"""


def plain_cookie(acc: Account) -> str | None:
    """取明文 Cookie 给驱动用。

    返回 None = 解不开（密钥丢失），调用方必须据此提示重新录入，
    不能当成空字符串继续跑。
    """
    enc = getattr(acc, "cookie_enc", "") or ""
    if enc:
        try:
            return decrypt(enc)
        except AiSecretError:
            return None
    # 迁移期兼容：还没加密过的旧数据仍从原字段读
    return getattr(acc, "cookie", "") or ""


def store_cookie(acc: Account, value: str) -> None:
    """写入 Cookie：加密进 cookie_enc，明文字段清空。空值表示"不改"。"""
    raw = (value or "").strip()
    if not raw:
        return
    acc.cookie_enc = encrypt(raw)
    acc.cookie = ""


def migrate_plaintext_cookies() -> int:
    """启动时把存量明文 Cookie 加密。

    升级路径必须不中断已有任务：加密前后 plain_cookie 取到的值一致，
    所以这里可以静默完成，只在日志里交代一句。
    """
    moved = 0
    with session_scope() as session:
        for acc in session.exec(select(Account)).all():
            if acc.cookie and not acc.cookie_enc:
                acc.cookie_enc = encrypt(acc.cookie)
                acc.cookie = ""
                session.add(acc)
                moved += 1
    return moved
