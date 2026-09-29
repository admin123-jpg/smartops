"""告警聚合与通知（M4）。

流程：Alertmanager Webhook 进来 -> 归一化 -> Redis 去重 -> 写库 ->
（首次或达到升级条件时）调大模型做根因诊断 -> 多渠道通知。

去重设计（面试常问）：
  fingerprint = sha1(hostname + alert_name + metric)
  用 Redis 的 SET NX EX 做「5 分钟窗口内首次出现才通知」，
  窗口内重复的只累加 repeat_count，不重复打扰值班同学。
  持续未恢复超过 ALERT_ESCALATE_MINUTES 则标记升级，追加通知一次。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

import requests
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models.alert import ALERT_FIRING, ALERT_RESOLVED, SEV_CRITICAL, SEV_INFO, SEV_WARNING, Alert
from app.services import llm_client
from app.utils import redis_client

log = logging.getLogger(__name__)

SEVERITY_MAP = {
    "critical": SEV_CRITICAL, "fatal": SEV_CRITICAL, "page": SEV_CRITICAL,
    "warning": SEV_WARNING, "warn": SEV_WARNING, "major": SEV_WARNING,
    "info": SEV_INFO, "none": SEV_INFO, "minor": SEV_INFO,
}


def make_fingerprint(hostname: str, alert_name: str, metric: str) -> str:
    raw = f"{hostname}|{alert_name}|{metric}".lower().encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:32]


def normalize_alertmanager(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Alertmanager 的 webhook 报文拍平成统一的告警字典列表。"""
    normalized: list[dict[str, Any]] = []
    for item in payload.get("alerts", []) or []:
        labels = item.get("labels") or {}
        annotations = item.get("annotations") or {}
        hostname = labels.get("instance") or labels.get("host") or labels.get("hostname") or "unknown"
        alert_name = labels.get("alertname") or "unnamed-alert"
        metric = labels.get("metric") or labels.get("__name__") or labels.get("job") or ""
        severity = SEVERITY_MAP.get(str(labels.get("severity", "")).lower(), SEV_WARNING)
        normalized.append({
            "hostname": hostname,
            "alert_name": alert_name,
            "metric": metric,
            "severity": severity,
            "summary": annotations.get("summary") or alert_name,
            "description": annotations.get("description") or "",
            "labels": labels,
            "status": ALERT_RESOLVED if item.get("status") == "resolved" else ALERT_FIRING,
            "starts_at": item.get("startsAt") or "",
            "fingerprint": make_fingerprint(hostname, alert_name, metric),
        })
    return normalized


def ingest(db: Session, payload: dict[str, Any], diagnose: bool = True) -> dict[str, Any]:
    """处理一批告警，返回统计结果。"""
    items = normalize_alertmanager(payload)
    created, deduped, resolved = 0, 0, 0
    window = settings.ALERT_DEDUP_WINDOW

    for item in items:
        existing = (
            db.query(Alert)
            .filter(Alert.fingerprint == item["fingerprint"], Alert.status == ALERT_FIRING)
            .order_by(Alert.id.desc())
            .first()
        )

        # ---------- 恢复通知 ----------
        if item["status"] == ALERT_RESOLVED:
            if existing:
                existing.status = ALERT_RESOLVED
                existing.resolved_at = datetime.now()
                resolved += 1
            redis_client.delete(f"alert:dedup:{item['fingerprint']}")
            continue

        # ---------- 去重窗口 ----------
        dedup_key = f"alert:dedup:{item['fingerprint']}"
        if existing:
            # 同一条告警还在 firing：只累加次数，不新建、不重复通知
            existing.repeat_count += 1
            existing.last_seen_at = datetime.now()
            if diagnose:
                _maybe_escalate(existing)
            deduped += 1
            redis_client.incr_with_ttl(dedup_key, window)
            continue

        alert = Alert(
            fingerprint=item["fingerprint"],
            hostname=item["hostname"],
            alert_name=item["alert_name"],
            metric=item["metric"],
            severity=item["severity"],
            summary=item["summary"],
            description=item["description"],
            labels_json=json.dumps(item["labels"], ensure_ascii=False),
            status=ALERT_FIRING,
        )
        db.add(alert)
        db.flush()
        created += 1

        # ---------- AI 根因诊断 ----------
        if diagnose:
            _attach_diagnosis(db, alert)

        # ---------- 通知 ----------
        notified = notify(alert)
        if notified:
            alert.notified_at = datetime.now()

        redis_client.setex(dedup_key, window, "1")

    db.commit()
    return {"total": len(items), "created": created, "deduped": deduped, "resolved": resolved}


def _attach_diagnosis(db: Session, alert: Alert) -> None:
    """给一条告警补上 AI 根因与处置建议。"""
    recent = (
        db.query(Alert)
        .filter(Alert.hostname == alert.hostname, Alert.id != alert.id)
        .order_by(Alert.id.desc())
        .limit(5)
        .all()
    )
    recent_text = "\n".join(f"- {a.alert_name}（{a.metric or '无指标'}，重复 {a.repeat_count} 次）" for a in recent) or "（无）"
    ctx = {
        "hostname": alert.hostname,
        "alert_name": alert.alert_name,
        "metric": alert.metric,
        "severity": alert.severity,
        "summary": alert.summary,
        "description": alert.description,
        "repeat_count": alert.repeat_count,
        "labels": json.loads(alert.labels_json or "{}"),
        "recent_alerts_text": recent_text,
    }
    result = llm_client.diagnose_alert(ctx)
    alert.ai_root_cause = result.get("root_cause", "")
    alert.ai_suggestion = llm_client.render_suggestions(result)
    alert.ai_mode = result.get("mode", "")


def _maybe_escalate(alert: Alert) -> None:
    """持续未恢复的告警标记升级（真实场景再叠加通知上级）。"""
    if alert.escalated:
        return
    if alert.severity == SEV_CRITICAL:
        age = datetime.now() - (alert.first_seen_at or datetime.now())
        if age > timedelta(minutes=30):
            alert.escalated = 1


# ============================================================ 通知渠道
def _post(url: str, payload: dict[str, Any]) -> bool:
    try:
        resp = requests.post(url, json=payload, timeout=8)
        return resp.status_code == 200
    except Exception as exc:  # noqa: BLE001
        log.warning("通知发送失败 %s：%s", url, exc)
        return False


def notify(alert: Alert) -> bool:
    """按配置的渠道发送通知。未配置渠道时只落库，不影响主流程。"""
    text = (
        f"【{alert.severity.upper()}】{alert.alert_name}\n"
        f"主机：{alert.hostname}\n"
        f"摘要：{alert.summary}\n"
        f"AI 根因：{alert.ai_root_cause or '（未诊断）'}\n"
        f"处置建议：{(alert.ai_suggestion or '').splitlines()[0] if alert.ai_suggestion else '（无）'}"
    )
    sent = False

    webhook = os.getenv("DINGTALK_WEBHOOK", "")
    if webhook:
        sent |= _post(webhook, {"msgtype": "text", "text": {"content": text}})

    feishu = os.getenv("FEISHU_WEBHOOK", "")
    if feishu:
        sent |= _post(feishu, {"msg_type": "text", "content": {"text": text}})

    wecom = os.getenv("WECOM_WEBHOOK", "")
    if wecom:
        sent |= _post(wecom, {"msgtype": "text", "text": {"content": text}})

    if not sent:
        log.info("未配置通知渠道，告警仅落库：%s / %s", alert.hostname, alert.alert_name)
    return sent


def stats(db: Session) -> dict[str, Any]:
    """告警看板数据：今日告警数、Top 主机、趋势、AI 诊断覆盖率。"""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_total = db.query(Alert).filter(Alert.first_seen_at >= today).count()
    firing = db.query(Alert).filter(Alert.status == ALERT_FIRING).count()

    top_hosts = (
        db.query(Alert.hostname, func.count(Alert.id).label("cnt"))
        .group_by(Alert.hostname)
        .order_by(text("cnt DESC"))
        .limit(5)
        .all()
    )
    all_alerts = db.query(Alert).all()
    diagnosed = sum(1 for a in all_alerts if a.ai_root_cause)

    trend: dict[str, int] = {}
    for alert in all_alerts:
        if alert.first_seen_at:
            key = alert.first_seen_at.strftime("%m-%d")
            trend[key] = trend.get(key, 0) + 1

    return {
        "today_total": today_total,
        "firing": firing,
        "total": len(all_alerts),
        "diagnosed": diagnosed,
        "diagnose_rate": round(diagnosed / len(all_alerts) * 100, 1) if all_alerts else 0.0,
        "top_hosts": [{"hostname": h, "count": c} for h, c in top_hosts],
        "trend": dict(sorted(trend.items())),
    }
