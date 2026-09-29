"""告警聚合接口（M4）。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.alert import ALERT_FIRING, SEV_CRITICAL, SEV_INFO, SEV_WARNING, Alert
from app.schemas.alert import AlertOut, AlertStats
from app.services import alert_handler, llm_client
from app.utils.response import ok
from app.utils.security import CurrentUser, RequireOps

router = APIRouter(prefix="/api/alerts", tags=["告警聚合"])


@router.post("/webhook", summary="接收 Alertmanager Webhook（无需登录，用 token 校验）")
def webhook(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    """对接 Prometheus Alertmanager。

    生产上应在 Alertmanager 里配 http_config.bearer_token，这里用 ?token= 校验。
    settings.ALERT_WEBHOOK_TOKEN 为空时不校验（方便本地调试）。
    """
    if settings.ALERT_WEBHOOK_TOKEN:
        token = request.query_params.get("token", "")
        if token != settings.ALERT_WEBHOOK_TOKEN:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "webhook token 不正确")

    result = alert_handler.ingest(db, payload, diagnose=True)
    return ok(result)


@router.get("", summary="告警列表")
def list_alerts(_: CurrentUser, db: Session = Depends(get_db),
                status_filter: str = Query("", alias="status"),
                severity: str = "", hostname: str = "",
                page: int = 1, page_size: int = 20) -> dict:
    query = db.query(Alert)
    if status_filter:
        query = query.filter(Alert.status == status_filter)
    if severity:
        query = query.filter(Alert.severity == severity)
    if hostname:
        query = query.filter(Alert.hostname.like(f"%{hostname}%"))
    total = query.count()
    rows = query.order_by(Alert.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({"items": [AlertOut.model_validate(a).model_dump() for a in rows],
               "total": total, "page": page, "page_size": page_size})


@router.get("/stats", summary="告警看板数据")
def alert_stats(_: CurrentUser, db: Session = Depends(get_db)) -> dict:
    return ok(AlertStats(**alert_handler.stats(db)).model_dump())


@router.get("/{alert_id}", summary="告警详情（含 AI 根因与处置建议）")
def get_alert(alert_id: int, _: CurrentUser, db: Session = Depends(get_db)) -> dict:
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "告警不存在")
    return ok(AlertOut.model_validate(alert).model_dump())


@router.post("/{alert_id}/diagnose", summary="对单条告警重新做 AI 诊断")
def diagnose(alert_id: int, _: RequireOps, db: Session = Depends(get_db)) -> dict:
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "告警不存在")
    alert_handler._attach_diagnosis(db, alert)
    db.commit()
    return ok({
        "alert_id": alert.id,
        "ai_root_cause": alert.ai_root_cause,
        "ai_suggestion": alert.ai_suggestion,
        "ai_mode": alert.ai_mode,
        "llm_configured": llm_client.is_configured(),
    })


@router.post("/mock", summary="生成模拟告警（演示 / 自测用，不需要 Prometheus）")
def mock_alert(_: RequireOps, count: int = 3, db: Session = Depends(get_db)) -> dict:
    """本地没有 Prometheus 时，用它造几条真实结构的告警来验证去重、诊断与通知链路。"""
    samples = [
        ("node1", "HostDiskWillFillIn4Hours", "node_filesystem_avail_bytes", SEV_CRITICAL,
         "磁盘 4 小时内预计写满", "文件系统 / 使用率 84%，可用空间持续下降"),
        ("node2", "HostHighMemUsage", "node_memory_MemAvailable_bytes", SEV_WARNING,
         "内存使用率偏高", "可用内存低于 20%"),
        ("master", "NodeClockSkewDetected", "node_timex_offset_seconds", SEV_INFO,
         "节点时钟偏移", "与 NTP 源偏差超过 0.05 秒"),
        ("node1", "KubeletDown", "up", SEV_CRITICAL,
         "kubelet 不可达", "kubelet 抓取目标 up == 0"),
    ]
    payload = {"alerts": []}
    for i in range(max(1, min(count, 8))):
        host, name, metric, sev, summary, desc = samples[i % len(samples)]
        payload["alerts"].append({
            "status": "firing",
            "labels": {"alertname": name, "instance": host, "metric": metric,
                       "severity": sev, "job": "node-exporter"},
            "annotations": {"summary": summary, "description": desc},
            "startsAt": (datetime.now() - timedelta(minutes=random.randint(2, 40))).isoformat(),
        })
    result = alert_handler.ingest(db, payload, diagnose=True)
    return ok(result)


@router.post("/{alert_id}/resolve", summary="手动标记告警已恢复")
def resolve(alert_id: int, _: RequireOps, db: Session = Depends(get_db)) -> dict:
    alert = db.get(Alert, alert_id)
    if not alert:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "告警不存在")
    alert.status = "resolved"
    alert.resolved_at = datetime.now()
    db.commit()
    return ok({"alert_id": alert.id, "status": alert.status})
