"""JWT 鉴权 + 基于角色的访问控制（RBAC）。

密码哈希用标准库 hashlib.pbkdf2_hmac 实现（不引第三方库，少一层依赖风险）：
  stored = pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>
"""
from __future__ import annotations

import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.user import ROLE_ADMIN, ROLE_OPS, ROLE_READONLY, User

_ITERATIONS = 120_000
_ALGO = "pbkdf2_sha256"

# 角色能力表：越靠上权限越大
ROLE_RANK = {ROLE_READONLY: 0, ROLE_OPS: 1, ROLE_ADMIN: 2}


# ---------------------------------------------------------------- 密码
def hash_password(raw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGO}${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(raw: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", raw.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ---------------------------------------------------------------- JWT
def create_access_token(username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


# ---------------------------------------------------------------- 依赖
_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    """从 Authorization: Bearer <token> 解析当前用户。

    为了便于前端调试，也支持 ?token= 查询参数（仅调试用）。
    """
    token = cred.credentials if cred else request.query_params.get("token")
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少访问令牌")
    try:
        payload = decode_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌已过期，请重新登录")
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌无效")

    user = db.query(User).filter(User.username == payload.get("sub")).first()
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在或已禁用")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(min_role: str):
    """依赖工厂：要求当前用户角色 >= min_role。"""

    def _checker(user: CurrentUser) -> User:
        if ROLE_RANK.get(user.role, -1) < ROLE_RANK.get(min_role, 99):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"需要 {min_role} 及以上权限")
        return user

    return _checker


RequireOps = Annotated[User, Depends(require_role(ROLE_OPS))]
RequireAdmin = Annotated[User, Depends(require_role(ROLE_ADMIN))]
