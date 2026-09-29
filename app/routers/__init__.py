"""路由包。"""
from app.routers import alerts, assets, auth, cicd, dashboard, executor, inspection, logs

__all__ = ["auth", "assets", "executor", "inspection", "alerts", "logs", "cicd", "dashboard"]
