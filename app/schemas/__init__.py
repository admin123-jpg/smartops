"""Pydantic 模型汇总。"""
from app.schemas.alert import AlertIngestResult, AlertOut, AlertStats
from app.schemas.asset import AssetCreate, AssetOut, AssetUpdate, ProbeResult
from app.schemas.auth import LoginRequest, TokenResponse, UserCreate, UserOut
from app.schemas.inspection import (
    InspectionRunCreate,
    InspectionRunOut,
    InspectionTemplateCreate,
    InspectionTemplateOut,
)
from app.schemas.task import TaskCreate, TaskOut, TaskTargetResultOut

__all__ = [
    "LoginRequest", "TokenResponse", "UserCreate", "UserOut",
    "AssetCreate", "AssetUpdate", "AssetOut", "ProbeResult",
    "TaskCreate", "TaskOut", "TaskTargetResultOut",
    "InspectionTemplateCreate", "InspectionTemplateOut",
    "InspectionRunCreate", "InspectionRunOut",
    "AlertOut", "AlertStats", "AlertIngestResult",
]
