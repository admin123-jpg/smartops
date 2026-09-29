"""资产管理：服务器、分组、变更留痕。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class AssetGroup(Base):
    __tablename__ = "asset_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    assets: Mapped[list["Asset"]] = relationship(back_populates="group")


class Asset(Base):
    """一台被管服务器。探测字段（os/kernel/cpu/mem/disk）由 SSH 采集填充。"""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hostname: Mapped[str] = mapped_column(String(128), default="")
    ip: Mapped[str] = mapped_column(String(64), index=True)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(64), default="root")
    auth_type: Mapped[str] = mapped_column(String(16), default="password")  # password | key
    password: Mapped[str] = mapped_column(String(255), default="")
    private_key_path: Mapped[str] = mapped_column(String(255), default="")

    group_id: Mapped[int | None] = mapped_column(ForeignKey("asset_groups.id"), nullable=True, index=True)
    env: Mapped[str] = mapped_column(String(32), default="prod")   # prod / test / dev
    purpose: Mapped[str] = mapped_column(String(128), default="")
    tags: Mapped[str] = mapped_column(String(255), default="")

    # ---- SSH 探测结果 ----
    os_version: Mapped[str] = mapped_column(String(128), default="")
    kernel: Mapped[str] = mapped_column(String(128), default="")
    cpu_cores: Mapped[int] = mapped_column(Integer, default=0)
    memory_mb: Mapped[int] = mapped_column(Integer, default=0)
    disk_total_gb: Mapped[float] = mapped_column(Float, default=0.0)
    disk_used_percent: Mapped[float] = mapped_column(Float, default=0.0)
    uptime: Mapped[str] = mapped_column(String(128), default="")

    status: Mapped[str] = mapped_column(String(16), default="unknown")  # online / offline / unknown
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_probe_error: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    group: Mapped[AssetGroup | None] = relationship(back_populates="assets")


class AssetChangeLog(Base):
    """资产变更留痕：谁、什么时候、把哪个字段从什么改成了什么。"""

    __tablename__ = "asset_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
