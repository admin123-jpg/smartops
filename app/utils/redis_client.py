"""Redis 客户端（懒连接 + 优雅降级）。

用途：Celery broker、告警去重窗口、任务实时进度缓存。
Redis 不可用时不让整个平台挂掉 —— 相关能力自动退化为进程内实现。
"""
from __future__ import annotations

import logging
import threading
import time

from app.config import settings

log = logging.getLogger(__name__)

_client = None
_lock = threading.Lock()
_unavailable_until = 0.0
_RETRY_INTERVAL = 30.0


def get_redis():
    """返回 redis 客户端；连不上时返回 None（调用方需处理）。"""
    global _client, _unavailable_until

    if _client is not None:
        return _client
    if time.time() < _unavailable_until:
        return None

    with _lock:
        if _client is not None:
            return _client
        if time.time() < _unavailable_until:
            return None
        try:
            import redis

            c = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_timeout=2)
            c.ping()
            _client = c
            log.info("Redis 已连接：%s", settings.REDIS_URL)
        except Exception as exc:  # noqa: BLE001
            _unavailable_until = time.time() + _RETRY_INTERVAL
            log.warning("Redis 不可用（%s），相关能力降级为进程内实现", exc)
            return None
    return _client


def redis_ok() -> bool:
    return get_redis() is not None


# ----------------------------------------------------------- 进程内降级实现
_fallback: dict[str, tuple[float, str]] = {}


def setex(key: str, seconds: int, value: str) -> None:
    c = get_redis()
    if c:
        c.setex(key, seconds, value)
    else:
        _fallback[key] = (time.time() + seconds, value)


def get(key: str) -> str | None:
    c = get_redis()
    if c:
        return c.get(key)
    item = _fallback.get(key)
    if not item:
        return None
    expire_at, value = item
    if time.time() > expire_at:
        _fallback.pop(key, None)
        return None
    return value


def setnx_ex(key: str, value: str, seconds: int) -> bool:
    """原子的「不存在才设置」，用于告警去重窗口。返回 True 表示本次是新告警。"""
    c = get_redis()
    if c:
        return bool(c.set(key, value, nx=True, ex=seconds))
    if get(key) is not None:
        return False
    _fallback[key] = (time.time() + seconds, value)
    return True


def incr_with_ttl(key: str, seconds: int) -> int:
    """计数 + 首次写入时设置过期，用于统计重复告警次数。"""
    c = get_redis()
    if c:
        pipe = c.pipeline()
        pipe.incr(key)
        pipe.expire(key, seconds)
        return int(pipe.execute()[0])
    item = _fallback.get(key)
    if not item or time.time() > item[0]:
        _fallback[key] = (time.time() + seconds, "1")
        return 1
    expire_at, value = item
    n = int(value) + 1
    _fallback[key] = (expire_at, str(n))
    return n


def publish(channel: str, message: str) -> None:
    """任务进度广播通道（Celery worker -> Web 进程）。"""
    c = get_redis()
    if c:
        c.publish(channel, message)


def delete(key: str) -> None:
    c = get_redis()
    if c:
        c.delete(key)
    else:
        _fallback.pop(key, None)
