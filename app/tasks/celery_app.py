"""Celery 应用与任务包。

设计取舍（面试会问「为什么用 Celery」）：
  批量操作 50 台机器、跑一次全量巡检，都是几十秒到几分钟的长任务。
  放在 HTTP 请求里同步做，页面会一直转圈、Nginx 可能 504。
  所以拆成：Web 只负责创建任务并返回 task_id，Celery worker 在后台跑，
  每跑完一台就把结果推出去（Redis Pub/Sub + 进度快照），前端订阅后实时刷新。

本地没有 Redis 时的处理：
  Celery 需要 broker。为了让「本机直接跑通」不被 Redis 卡住，
  启动时探测一次 Redis：连不上就切到 task_always_eager（任务在进程内同步执行），
  功能完整但失去异步性 —— 日志里会明确提示，不会静默改变行为。
"""
from __future__ import annotations

import logging

from celery import Celery

from app.config import settings

log = logging.getLogger(__name__)

celery_app = Celery(
    "smartops",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["app.tasks.batch_exec", "app.tasks.inspection"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=False,
    task_track_started=True,
    worker_max_tasks_per_child=200,        # 防内存泄漏累积
    task_acks_late=True,                   # 任务执行完才 ack，worker 崩了能重投
    broker_connection_retry_on_startup=True,
    result_expires=3600,
)

# 巡检定时任务（生产用 celery beat；本机演示也可用 /inspection/run 手动触发）
celery_app.conf.beat_schedule = {
    "daily-inspection": {
        "task": "app.tasks.inspection.scheduled_inspection",
        "schedule": 60 * 60 * 6,   # 每 6 小时一次
    },
}


def _detect_broker() -> bool:
    """启动时探测 broker 是否可用，决定是否降级为进程内执行。"""
    try:
        import redis

        client = redis.Redis.from_url(settings.REDIS_URL, socket_timeout=2)
        client.ping()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Celery broker(%s) 不可用：%s", settings.REDIS_URL, exc)
        return False


BROKER_AVAILABLE = _detect_broker()

if not BROKER_AVAILABLE:
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = False
    log.warning("已切换 Celery 为 eager 模式：任务在进程内同步执行（功能不变，失去异步性）")


def broker_status() -> dict:
    return {
        "broker_available": BROKER_AVAILABLE,
        "eager_mode": bool(celery_app.conf.task_always_eager),
        "broker_url": settings.REDIS_URL,
    }
