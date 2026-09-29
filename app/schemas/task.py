"""批量执行相关模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    asset_ids: list[int] = Field(..., min_length=1, description="目标资产 ID 列表")
    command: str = Field(..., min_length=1, max_length=8000, description="要执行的命令或脚本内容")
    name: str = ""
    task_type: str = Field("exec", pattern="^(exec|script|service)$")
    confirmed: bool = Field(False, description="命中高危关键词时必须显式确认")


class TaskTargetResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    asset_id: int
    asset_label: str
    status: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    duration_ms: int = 0


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    task_type: str
    command: str
    target_count: int
    status: str
    need_confirm: int
    operator: str
    success_count: int
    failed_count: int
    created_at: datetime | None = None
    finished_at: datetime | None = None


class TaskDetailOut(TaskOut):
    results: list[TaskTargetResultOut] = []
