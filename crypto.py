"""Session 字符串的对称加解密。

密钥只存在于 .env（chmod 600），不进数据库。
即使 tgsaver.db 被整个拖走，里面的 session 也解不开。
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from config import CFG

_f = Fernet(CFG.secret_key.encode())


def encrypt(plain: str) -> bytes:
    return _f.encrypt(plain.encode())


def decrypt(token: bytes) -> str:
    try:
        return _f.decrypt(token).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "session 解密失败：SECRET_KEY 与数据库不匹配。"
            "如果你换过密钥，需要重新运行 login.py 登录。"
        ) from e
