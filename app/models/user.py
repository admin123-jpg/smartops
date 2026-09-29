"""用户与审计日志。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# 角色 -> 可执行的操作范围（RBAC 模型，见 app/utils/security.py）
ROLE_ADMIN = "admin"        # 全部权限，含用户管理
ROLE_OPS = "ops"            # 资产/批量执行/巡检/告警，无用户管理
ROLE_READONLY = "readonly"  # 只读


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default=ROLE_READONLY)
    is_active: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    """操作审计：所有批量操作、资产变更、权限变更都留痕。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operator: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)   # login / batch_exec / asset_update ...
    target: Mapped[str] = mapped_column(String(255), default="")  # 目标描述，如 "asset:12,13"
    detail: Mapped[str] = mapped_column(Text, default="")
    client_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)


class AssetGroupMember(Base):
    """预留：用户对资产组的可见范围（按组授权）。"""

    __tablename__ = "user_group_scope"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("asset_groups.id"), index=True)
