"""批量执行任务与逐机结果。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_SUCCESS = "success"
TASK_PARTIAL = "partial"
TASK_FAILED = "failed"


class TaskRecord(Base):
    """一次批量操作（下发命令 / 推送脚本 / 服务启停）。"""

    __tablename__ = "task_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_type: Mapped[str] = mapped_column(String(32), default="exec")  # exec | script | service | upload
    name: Mapped[str] = mapped_column(String(128), default="")
    command: Mapped[str] = mapped_column(Text, default="")
    asset_ids: Mapped[str] = mapped_column(Text, default="")   # 逗号分隔，便于审计回溯
    target_count: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(16), default=TASK_PENDING, index=True)
    need_confirm: Mapped[int] = mapped_column(Integer, default=0)   # 是否命中高危关键词
    confirmed_by: Mapped[str] = mapped_column(String(64), default="")
    operator: Mapped[str] = mapped_column(String(64), default="")

    celery_task_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    results: Mapped[list["TaskTargetResult"]] = relationship(back_populates="task")


class TaskTargetResult(Base):
    """单台机器的执行结果。WebSocket 每完成一台就推一条。"""

    __tablename__ = "task_target_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("task_records.id"), index=True)
    asset_id: Mapped[int] = mapped_column(Integer, index=True)
    asset_label: Mapped[str] = mapped_column(String(128), default="")  # ip/hostname 快照

    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/running/success/failed
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(255), default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    task: Mapped[TaskRecord] = relationship(back_populates="results")
