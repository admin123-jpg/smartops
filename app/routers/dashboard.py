"""总览看板 与 AI 能力状态。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.alert import ALERT_FIRING, Alert
from app.models.asset import Asset
from app.models.inspection import InspectionRun
from app.models.task import TaskRecord
from app.services import alert_handler, llm_client
from app.tasks.celery_app import broker_status
from app.utils import redis_client
from app.utils.response import ok
from app.utils.security import CurrentUser

router = APIRouter(prefix="/api/dashboard", tags=["总览看板"])
ai_router = APIRouter(prefix="/api/ai", tags=["AI 能力"])


@router.get("/overview", summary="首页总览")
def overview(_: CurrentUser, db: Session = Depends(get_db)) -> dict:
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    total_assets = db.query(Asset).count()
    online = db.query(Asset).filter(Asset.status == "online").count()
    offline = db.query(Asset).filter(Asset.status == "offline").count()

    tasks_total = db.query(TaskRecord).count()
    tasks_today = db.query(TaskRecord).filter(TaskRecord.created_at >= today).count()
    running = db.query(TaskRecord).filter(TaskRecord.status == "running").count()

    runs_total = db.query(InspectionRun).count()
    last_run = db.query(InspectionRun).order_by(InspectionRun.id.desc()).first()

    alert_stat = alert_handler.stats(db)

    # 资源水位 Top（磁盘使用率最高的几台）
    top_disk = (
        db.query(Asset.hostname, Asset.ip, Asset.disk_used_percent)
        .filter(Asset.status == "online")
        .order_by(Asset.disk_used_percent.desc())
        .limit(5)
        .all()
    )

    return ok({
        "assets": {"total": total_assets, "online": online,
                   "offline": offline, "unknown": max(0, total_assets - online - offline)},
        "tasks": {"total": tasks_total, "today": tasks_today, "running": running},
        "inspection": {
            "total": runs_total,
            "last": {
                "id": last_run.id,
                "template": last_run.template_name,
                "ok": last_run.ok_items,
                "warn": last_run.warn_items,
                "critical": last_run.critical_items,
                "finished_at": last_run.finished_at.strftime("%Y-%m-%d %H:%M:%S") if last_run.finished_at else "",
                "report_path": last_run.report_path,
            } if last_run else None,
        },
        "alerts": alert_stat,
        "top_disk": [{"hostname": h or i, "ip": i, "used_percent": p} for h, i, p in top_disk],
        "system": system_status(),
        "llm": llm_status(),
    })


@router.get("/system", summary="平台自身运行状态")
def system_only(_: CurrentUser) -> dict:
    return ok(system_status())


def system_status() -> dict:
    broker = broker_status()
    return {
        "database": settings.DATABASE_URL.split("://")[0],
        "redis_available": redis_client.redis_ok(),
        "redis_url": settings.REDIS_URL,
        "celery": broker,
        "async_mode": "同步（eager）" if broker["eager_mode"] else "异步（Celery worker）",
        "dangerous_keywords": list(settings.DANGEROUS_KEYWORDS),
    }


@ai_router.get("/status", summary="大模型接入状态")
def ai_status(_: CurrentUser) -> dict:
    return ok(llm_status())


def llm_status() -> dict:
    configured = llm_client.is_configured()
    return {
        "configured": configured,
        "provider": settings.LLM_PROVIDER,
        "model": settings.LLM_MODEL,
        "base_url": settings.LLM_BASE_URL,
        "mode": "大模型" if configured else "规则库降级",
        "hint": "" if configured else "未配置 LLM_API_KEY，AI 诊断会自动降级为内置规则库",
    }


@ai_router.get("/capabilities", summary="AI 在哪几个环节生效")
def ai_capabilities(_: CurrentUser) -> dict:
    return ok([
        {"scene": "告警根因诊断", "input": "Alertmanager 告警上下文 + 同主机近期告警",
         "output": "根因 / 证据链 / 按优先级排序的处置步骤（带风险等级）"},
        {"scene": "巡检结论总结", "input": "一次巡检的全部异常项清单",
         "output": "整体结论 + 优先处理项，附加到 HTML 报告顶部"},
        {"scene": "日志根因分析", "input": "命中错误关键字的日志片段 + 归一化聚合结果",
         "output": "根因判断 + 可直接执行的处置命令"},
    ])
