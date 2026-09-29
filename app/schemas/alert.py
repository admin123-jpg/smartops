"""告警相关模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    hostname: str
    alert_name: str
    metric: str
    severity: str
    summary: str
    description: str
    status: str
    repeat_count: int
    escalated: int
    ai_root_cause: str
    ai_suggestion: str
    ai_mode: str
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    resolved_at: datetime | None = None


class AlertStats(BaseModel):
    today_total: int
    firing: int
    total: int
    diagnosed: int
    diagnose_rate: float
    top_hosts: list[dict] = []
    trend: dict = {}


class AlertIngestResult(BaseModel):
    total: int
    created: int
    deduped: int
    resolved: int
