"""资产相关模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AssetGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = ""


class AssetGroupOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str


class AssetBase(BaseModel):
    ip: str = Field(..., description="管理 IP")
    hostname: str = ""
    ssh_port: int = Field(22, ge=1, le=65535)
    ssh_user: str = "root"
    auth_type: str = Field("password", pattern="^(password|key)$")
    password: str = ""
    private_key_path: str = ""
    group_id: int | None = None
    env: str = "prod"
    purpose: str = ""
    tags: str = ""


class AssetCreate(AssetBase):
    pass


class AssetUpdate(BaseModel):
    hostname: str | None = None
    ssh_port: int | None = None
    ssh_user: str | None = None
    auth_type: str | None = None
    password: str | None = None
    private_key_path: str | None = None
    group_id: int | None = None
    env: str | None = None
    purpose: str | None = None
    tags: str | None = None


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ip: str
    hostname: str
    ssh_port: int
    ssh_user: str
    auth_type: str
    group_id: int | None = None
    env: str
    purpose: str
    tags: str
    os_version: str
    kernel: str
    cpu_cores: int
    memory_mb: int
    disk_total_gb: float
    disk_used_percent: float
    uptime: str
    status: str
    last_probe_at: datetime | None = None
    last_probe_error: str
    created_at: datetime | None = None


class ProbeResult(BaseModel):
    asset_id: int
    ok: bool
    data: dict = {}
    error: str = ""
    duration_ms: int = 0
