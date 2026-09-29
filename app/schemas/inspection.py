"""巡检相关模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class InspectionTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    checks: list[dict] = Field(default_factory=list, description="检查项定义列表")


class InspectionTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    checks_json: str
    is_builtin: int
    created_at: datetime | None = None


class InspectionRunCreate(BaseModel):
    template_id: int | None = None
    asset_ids: list[int] = Field(default_factory=list, description="为空则巡检全部在线资产")


class InspectionResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    asset_id: int
    asset_label: str
    item_type: str
    item_name: str
    level: str
    value: str
    message: str
    suggestion: str


class InspectionRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    template_id: int
    template_name: str
    asset_ids: str
    status: str
    trigger: str
    total_items: int
    ok_items: int
    warn_items: int
    critical_items: int
    report_path: str
    ai_summary: str
    operator: str
    created_at: datetime | None = None
    finished_at: datetime | None = None
