"""ORM 模型汇总：import 本包即完成所有表注册。"""
from app.models.alert import Alert
from app.models.asset import Asset, AssetChangeLog, AssetGroup
from app.models.inspection import InspectionResult, InspectionRun, InspectionTemplate
from app.models.task import TaskRecord, TaskTargetResult
from app.models.user import AuditLog, User

__all__ = [
    "User",
    "AuditLog",
    "AssetGroup",
    "Asset",
    "AssetChangeLog",
    "TaskRecord",
    "TaskTargetResult",
    "InspectionTemplate",
    "InspectionRun",
    "InspectionResult",
    "Alert",
]
