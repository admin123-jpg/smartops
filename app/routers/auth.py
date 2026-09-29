"""登录鉴权与用户管理。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.user import ROLE_ADMIN, ROLE_OPS, ROLE_READONLY, AuditLog, User
from app.schemas.auth import LoginRequest, TokenResponse, UserCreate, UserOut
from app.utils.response import ok
from app.utils.security import (
    CurrentUser,
    RequireAdmin,
    create_access_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["认证"])


def write_audit(db: Session, operator: str, action: str, target: str = "",
                detail: str = "", client_ip: str = "") -> None:
    db.add(AuditLog(operator=operator, action=action, target=target, detail=detail, client_ip=client_ip))
    db.commit()


@router.post("/login", summary="登录换取 JWT")
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> dict:
    user = db.query(User).filter(User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已被禁用")

    user.last_login_at = datetime.now()
    db.commit()
    token = create_access_token(user.username, user.role)
    write_audit(db, user.username, "login", target=user.username,
                client_ip=request.client.host if request.client else "")
    return ok(TokenResponse(
        access_token=token,
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
        username=user.username,
        role=user.role,
    ).model_dump())


@router.get("/me", summary="当前登录用户")
def me(user: CurrentUser) -> dict:
    return ok(UserOut.model_validate(user).model_dump())


@router.get("/users", summary="用户列表（管理员）")
def list_users(_: RequireAdmin, db: Session = Depends(get_db)) -> dict:
    users = db.query(User).order_by(User.id).all()
    return ok([UserOut.model_validate(u).model_dump() for u in users])


@router.post("/users", summary="新建用户（管理员）")
def create_user(payload: UserCreate, admin: RequireAdmin, db: Session = Depends(get_db)) -> dict:
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "用户名已存在")
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name or payload.username,
        role=payload.role,
    )
    db.add(user)
    db.commit()
    write_audit(db, admin.username, "user_create", target=payload.username, detail=f"role={payload.role}")
    return ok(UserOut.model_validate(user).model_dump())


@router.patch("/users/{user_id}", summary="改角色 / 启停（管理员）")
def update_user(user_id: int, role: str | None = None, is_active: int | None = None,
                admin: RequireAdmin = None, db: Session = Depends(get_db)) -> dict:
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if role:
        if role not in (ROLE_ADMIN, ROLE_OPS, ROLE_READONLY):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "非法角色")
        user.role = role
    if is_active is not None:
        user.is_active = is_active
    db.commit()
    write_audit(db, admin.username, "user_update", target=user.username,
                detail=f"role={user.role} active={user.is_active}")
    return ok(UserOut.model_validate(user).model_dump())


@router.get("/audit", summary="操作审计日志（管理员）")
def audit_logs(page: int = 1, page_size: int = 20, action: str = "",
               _: RequireAdmin = None, db: Session = Depends(get_db)) -> dict:
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    total = query.count()
    rows = query.order_by(AuditLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({
        "items": [
            {"id": r.id, "operator": r.operator, "action": r.action, "target": r.target,
             "detail": r.detail, "client_ip": r.client_ip,
             "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""}
            for r in rows
        ],
        "total": total, "page": page, "page_size": page_size,
    })
