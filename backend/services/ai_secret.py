"""AI 凭据的本地加密：密钥文件只落在数据目录，明文不入库。

设计约束（agent-design.md 第十二节 RK-12）：
- Key 必须加密存储，界面只回显掩码；
- 密钥不硬编码、不进镜像、不进代码库；
- 密钥文件丢失时必须给出明确路径提示，而不是静默失败。
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

from .. import config

_KEY_FILE = ".ai_secret.key"


class AiSecretError(RuntimeError):
    """凭据无法解密（通常是密钥文件丢失或被替换）。"""


def _key_path():
    return config.DATA_DIR / _KEY_FILE


def _fernet() -> Fernet:
    path = _key_path()
    data = path.read_bytes().strip() if path.exists() else b""
    if not data:
        data = Fernet.generate_key()
        path.write_bytes(data)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # 非 POSIX 或权限受限：不因为 chmod 失败就丢掉已生成的密钥
    return Fernet(data)


def encrypt(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise AiSecretError(f"凭据无法解密，请到「设置 → AI 助手」重新录入 API Key（密钥文件：{_key_path()}）") from exc


def mask(value: str) -> str:
    """掩码回显：只保留前 4 与后 4，中间用固定长度占位，不泄露原长。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * 6
    return f"{value[:4]}{'*' * 6}{value[-4:]}"
