"""日志分析工具（M5）。

两类日志分开处理：
- Nginx 访问日志：统计 Top IP、慢请求（$request_time 超阈值）、状态码分布、时间分布；
- 通用应用/系统日志：统计 ERROR/Exception 出现次数，并用「归一化指纹」把
  同一类错误聚合成一条（时间戳、IP、PID、数字等易变部分先抹掉再计算）。

归一化聚合的思路来自实战：同一类故障在日志里会以不同时间戳刷屏，
不做归一化就会得到几百条"独立错误"，看不出真正的 Top 问题。
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

LEVEL_KEYWORDS = ("error", "exception", "fatal", "critical", "failed", "fail", "denied", "refused")

# 归一化时抹掉的易变片段（顺序敏感，先长后短）
_NORMALIZE_RULES: list[tuple[str, str]] = [
    (r"\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?", "<TS>"),
    (r"\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b", "<TIME>"),
    (r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b", "<IP>"),
    (r"\b[0-9a-f]{8,}\b", "<HEX>"),
    (r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<UUID>"),
    (r"\b(?:pid|tid)[=: ]?\d+\b", "<PID>"),
    (r"/[\w./-]{4,}", "<PATH>"),
    (r"\b\d+\b", "<N>"),
    (r"\s+", " "),
]

_NGINX_RE = re.compile(
    r'^(?P<ip>\S+) \S+ \S+ \[(?P<time>[^\]]+)\] "(?P<method>\S+) (?P<url>\S+)[^"]*" '
    r"(?P<status>\d{3}) (?P<size>\S+)(?: \"(?P<referer>[^\"]*)\" \"(?P<ua>[^\"]*)\")?"
    r"(?:\s+(?P<rt>[\d.]+))?"
)


def normalize(text: str) -> str:
    """把一行日志归一化成「骨架」，用于同类错误聚合。"""
    out = text.strip().lower()
    for pattern, repl in _NORMALIZE_RULES:
        out = re.sub(pattern, repl, out)
    return out[:200]


def fingerprint(text: str) -> str:
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:16]


def parse_nginx(lines: list[str], slow_threshold: float = 1.0) -> dict[str, Any]:
    ips: Counter[str] = Counter()
    urls: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    slow: list[dict[str, Any]] = []
    hours: Counter[str] = Counter()
    matched = 0

    for line in lines:
        m = _NGINX_RE.match(line)
        if not m:
            continue
        matched += 1
        ips[m.group("ip")] += 1
        urls[m.group("url")] += 1
        statuses[m.group("status")] += 1
        ts = m.group("time")
        if len(ts) >= 14:
            hours[ts[12:14] + ":00"] += 1
        rt = m.group("rt")
        if rt:
            try:
                value = float(rt)
            except ValueError:
                continue
            if value >= slow_threshold:
                slow.append({"url": m.group("url"), "ip": m.group("ip"),
                             "status": m.group("status"), "request_time": value})

    slow.sort(key=lambda x: x["request_time"], reverse=True)
    return {
        "format": "nginx" if matched else "unknown",
        "matched_lines": matched,
        "top_ips": ips.most_common(10),
        "top_urls": urls.most_common(10),
        "status_distribution": dict(statuses.most_common()),
        "slow_requests": slow[:10],
        "slow_threshold": slow_threshold,
        "hour_distribution": dict(sorted(hours.items())),
    }


def parse_generic(lines: list[str]) -> dict[str, Any]:
    """通用日志：错误行统计 + 归一化聚合。"""
    error_lines = [ln for ln in lines if any(k in ln.lower() for k in LEVEL_KEYWORDS)]
    buckets: dict[str, dict[str, Any]] = {}

    for line in error_lines:
        fp = fingerprint(line)
        item = buckets.setdefault(fp, {"count": 0, "sample": line.strip()[:300], "normalized": normalize(line)})
        item["count"] += 1

    grouped = sorted(buckets.values(), key=lambda x: x["count"], reverse=True)
    return {
        "format": "generic",
        "total_lines": len(lines),
        "error_lines": len(error_lines),
        "unique_error_types": len(grouped),
        "top_errors": grouped[:10],
        "first_sample": error_lines[0].strip()[:300] if error_lines else "",
    }


def analyze(text: str, slow_threshold: float = 1.0, source: str = "") -> dict[str, Any]:
    """入口：自动判断日志格式并分析。"""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    nginx = parse_nginx(lines, slow_threshold)
    result: dict[str, Any]
    if nginx["matched_lines"] >= max(3, len(lines) * 0.3):
        result = nginx
    else:
        result = parse_generic(lines)
    result["source"] = source
    result["hit_keywords"] = sorted({k for ln in lines[:5000] for k in LEVEL_KEYWORDS if k in ln.lower()})
    return result


def build_llm_context(text: str, analysis: dict[str, Any]) -> dict[str, Any]:
    """把分析结果整理成大模型需要的上下文。"""
    excerpt_lines = [ln for ln in text.splitlines() if any(k in ln.lower() for k in LEVEL_KEYWORDS)]
    return {
        "source": analysis.get("source", ""),
        "hit_keywords": analysis.get("hit_keywords", []),
        "error_lines": analysis.get("error_lines") or analysis.get("matched_lines", 0),
        "log_excerpt": "\n".join(excerpt_lines[:80]),
    }
