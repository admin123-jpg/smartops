"""告警聚合：去重指纹、升级状态、AI 诊断结论。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

ALERT_FIRING = "firing"
ALERT_RESOLVED = "resolved"

SEV_INFO = "info"
SEV_WARNING = "warning"
SEV_CRITICAL = "critical"


class Alert(Base):
    """一条聚合后的告警。fingerprint 用于去重：同主机+同指标+同告警名 视为同一条。"""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    source: Mapped[str] = mapped_column(String(32), default="alertmanager")
    hostname: Mapped[str] = mapped_column(String(128), index=True)
    metric: Mapped[str] = mapped_column(String(128), default="")
    alert_name: Mapped[str] = mapped_column(String(128), default="")
    severity: Mapped[str] = mapped_column(String(16), default=SEV_WARNING)
    summary: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    labels_json: Mapped[str] = mapped_column(Text, default="{}")

    status: Mapped[str] = mapped_column(String(16), default=ALERT_FIRING, index=True)
    repeat_count: Mapped[int] = mapped_column(Integer, default=1)      # 被去重吞掉的次数
    escalated: Mapped[int] = mapped_column(Integer, default=0)         # 是否已升级通知

    # AI 诊断结果
    ai_root_cause: Mapped[str] = mapped_column(Text, default="")
    ai_suggestion: Mapped[str] = mapped_column(Text, default="")
    ai_mode: Mapped[str] = mapped_column(String(16), default="")        # llm | rule

    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
