"""自动化巡检：模板、执行批次、逐项结果。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"
LEVEL_ERROR = "error"


class InspectionTemplate(Base):
    """巡检模板：内置磁盘/内存/负载/进程/端口/日志六类检查项。"""

    __tablename__ = "inspection_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    # JSON 数组，每项形如 {"type":"disk","threshold":80,"level":"warn"}
    checks_json: Mapped[str] = mapped_column(Text, default="[]")
    is_builtin: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class InspectionRun(Base):
    """一次巡检执行（一键或定时触发）。"""

    __tablename__ = "inspection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("inspection_templates.id"), index=True)
    template_name: Mapped[str] = mapped_column(String(64), default="")
    asset_ids: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual | cron

    total_items: Mapped[int] = mapped_column(Integer, default=0)
    ok_items: Mapped[int] = mapped_column(Integer, default=0)
    warn_items: Mapped[int] = mapped_column(Integer, default=0)
    critical_items: Mapped[int] = mapped_column(Integer, default=0)

    report_path: Mapped[str] = mapped_column(String(255), default="")
    ai_summary: Mapped[str] = mapped_column(Text, default="")   # 大模型生成的巡检结论

    operator: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    results: Mapped[list["InspectionResult"]] = relationship(back_populates="run")


class InspectionResult(Base):
    """单个检查项的结果，异常项在报告里高亮。"""

    __tablename__ = "inspection_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("inspection_runs.id"), index=True)
    asset_id: Mapped[int] = mapped_column(Integer, index=True)
    asset_label: Mapped[str] = mapped_column(String(128), default="")

    item_type: Mapped[str] = mapped_column(String(32))     # disk / memory / load / process / port / log
    item_name: Mapped[str] = mapped_column(String(128))
    level: Mapped[str] = mapped_column(String(16), default=LEVEL_OK, index=True)
    value: Mapped[str] = mapped_column(String(255), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    suggestion: Mapped[str] = mapped_column(Text, default="")   # 修复建议（可由大模型补全）
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    run: Mapped[InspectionRun] = relationship(back_populates="results")
