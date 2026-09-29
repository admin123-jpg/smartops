"""自动化巡检引擎。

一次巡检 = 对每台目标执行一个「汇总采集脚本」，拿到原始值后按模板里的检查项逐条判级。

为什么把采集合并成一个脚本？
  一次 SSH 会话取回 6 类数据，比每项检查开一次连接快一个数量级，
  也避免在目标机上留下大量短连接（对 sshd 更友好）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from app.config import settings
from app.models.inspection import LEVEL_CRITICAL, LEVEL_ERROR, LEVEL_OK, LEVEL_WARN
from app.services import ssh_client
from app.services.ssh_client import Target

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- 内置检查项
BUILTIN_CHECKS: list[dict[str, Any]] = [
    {"type": "disk", "threshold": 80, "level": "warn", "name": "磁盘使用率"},
    {"type": "memory", "threshold": 85, "level": "warn", "name": "内存使用率"},
    {"type": "load", "threshold": 4, "level": "warn", "name": "CPU 负载(1分钟)"},
    {"type": "process", "names": ["sshd", "crond"], "level": "critical", "name": "关键进程存活"},
    {"type": "port", "ports": [22], "level": "critical", "name": "端口监听"},
    {"type": "log", "keywords": ["error", "exception", "fail", "denied"],
     "paths": ["/var/log/messages", "/var/log/secure", "/var/log/syslog"], "level": "warn", "name": "系统日志错误"},
]

LEVEL_RANK = {LEVEL_OK: 0, LEVEL_WARN: 1, LEVEL_CRITICAL: 2, LEVEL_ERROR: 3}


def build_script(checks: list[dict[str, Any]]) -> str:
    """根据检查项拼采集脚本（只采集模板里真正用到的数据，减少无关开销）。"""
    types = {c.get("type") for c in checks}
    parts: list[str] = []
    # 注意：这里一律用三引号包裹 shell 片段，避免 Python 转义把 awk 的引号弄坏
    # （踩过的坑：用 r"..." 时 \" 会原样保留反斜杠，导致远端 awk 语法错误）

    if "disk" in types:
        parts.append('''echo "###DISK"; df -P -x tmpfs -x devtmpfs -x overlay 2>/dev/null | awk 'NR>1{printf "%s|%s\\n", $6, $5}' ''')
    if "memory" in types:
        parts.append('''echo "###MEM"; free -m 2>/dev/null | awk '/Mem:/{printf "%s|%s\\n", $3, $2}' ''')
    if "load" in types:
        parts.append('''echo "###LOAD"; cat /proc/loadavg 2>/dev/null | awk '{print $1}' ''')
    if "process" in types:
        names = []
        for c in checks:
            if c.get("type") == "process":
                names.extend(c.get("names") or [])
        if names:
            body = "; ".join(
                f'if pgrep -x "{n}" >/dev/null 2>&1; then echo "{n}=1"; else echo "{n}=0"; fi'
                for n in dict.fromkeys(names)
            )
            parts.append(f'echo "###PROC"; {body}')
    if "port" in types:
        ports: list[int] = []
        for c in checks:
            if c.get("type") == "port":
                ports.extend(int(p) for p in (c.get("ports") or []))
        if ports:
            pattern = "|".join(f":{p}[[:space:]]" for p in dict.fromkeys(ports))
            parts.append('''echo "###PORT"; (ss -lnt 2>/dev/null || netstat -lnt 2>/dev/null) '''
                         f'''| grep -E "{pattern}" | awk '{{print $4}}' | head -20''')
    if "log" in types:
        paths: list[str] = []
        for c in checks:
            if c.get("type") == "log":
                paths.extend(c.get("paths") or [])
        if paths:
            joined = " ".join(dict.fromkeys(paths))
            parts.append(f'''echo "###LOG"; for f in {joined}; do [ -f "$f" ] && '''
                         '''tail -n 300 "$f" 2>/dev/null | grep -i -c -E "error|exception|fail|denied" '''
                         '''| sed "s|^|$f: |"; done''')

    script = "\n".join(parts)
    # 收尾必须补一条成功命令：
    # 踩过的坑 —— 最后一条是 `[ -f /var/log/syslog ] && ...` 时，CentOS 7 上没有这个文件，
    # 判断失败会让整个脚本退出码变成 1，巡检就被误判成「机器不可达」。
    # 用 ###END 作为哨兵：既是成功退出码，也便于识别脚本是否完整跑完。
    return f"#!/bin/sh\n{script}\necho \"###END\"\n"


def _parse_sections(stdout: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    for name, body in re.findall(r"###(\w+)\n(.*?)(?=\n###|\Z)", stdout, re.S):
        sections[name.lower()] = body.strip()
    return sections


def _evaluate(checks: list[dict[str, Any]], sections: dict[str, str]) -> list[dict[str, Any]]:
    """把采集到的原始值套上阈值，产出逐项结果。"""
    results: list[dict[str, Any]] = []

    for check in checks:
        ctype = check.get("type")
        name = check.get("name") or ctype
        default_level = check.get("level", LEVEL_WARN)

        # ---------- 磁盘 ----------
        if ctype == "disk":
            raw = sections.get("disk", "")
            threshold = float(check.get("threshold", settings.INSPECT_DISK_WARN))
            crit = float(check.get("critical_threshold", settings.INSPECT_DISK_CRIT))
            worst: tuple[str, str, str] = (LEVEL_OK, "", "")
            for token in raw.split():
                if "|" not in token:
                    continue
                mount, pct = token.split("|", 1)
                try:
                    value = float(pct.replace("%", ""))
                except ValueError:
                    continue
                if value >= crit:
                    worst = (LEVEL_CRITICAL, f"{mount} {value}%", f"{mount} 使用率 {value}% 已达严重水位（≥{crit}%）")
                elif value >= threshold and worst[0] == LEVEL_OK:
                    worst = (LEVEL_WARN, f"{mount} {value}%", f"{mount} 使用率 {value}% 超过阈值 {threshold}%")
            results.append(_mk(name, ctype, worst[0], worst[1] or "正常", worst[2] or f"各挂载点使用率均低于 {threshold}%"))

        # ---------- 内存 ----------
        elif ctype == "memory":
            raw = sections.get("mem", "")
            threshold = float(check.get("threshold", settings.INSPECT_MEM_WARN))
            used = total = 0.0
            if "|" in raw:
                try:
                    u, t = raw.split("|", 1)
                    used, total = float(u), float(t)
                except ValueError:
                    pass
            pct = round(used / total * 100, 1) if total else 0.0
            if pct >= threshold:
                results.append(_mk(name, ctype, LEVEL_WARN, f"{pct}%",
                                   f"内存使用率 {pct}%（{used:.0f}/{total:.0f} MB）超过阈值 {threshold}%"))
            else:
                results.append(_mk(name, ctype, LEVEL_OK, f"{pct}%", f"内存使用率 {pct}%，正常"))

        # ---------- 负载 ----------
        elif ctype == "load":
            raw = sections.get("load", "")
            threshold = float(check.get("threshold", settings.INSPECT_LOAD_WARN))
            try:
                value = float(raw.split()[0])
            except (ValueError, IndexError):
                value = 0.0
            level = LEVEL_WARN if value >= threshold else LEVEL_OK
            results.append(_mk(name, ctype, level, f"{value}",
                               f"1 分钟负载 {value}，超过阈值 {threshold}" if level != LEVEL_OK else f"1 分钟负载 {value}，正常"))

        # ---------- 进程 ----------
        elif ctype == "process":
            raw = sections.get("proc", "")
            alive = {line.split("=")[0] for line in raw.splitlines() if line.strip().endswith("=1")}
            expected = check.get("names") or []
            missing = [n for n in expected if n not in alive]
            if missing:
                results.append(_mk(name, ctype, default_level, f"缺失 {len(missing)} 个",
                                   f"以下关键进程未运行：{', '.join(missing)}"))
            else:
                results.append(_mk(name, ctype, LEVEL_OK, f"{len(expected)}/{len(expected)} 存活",
                                   f"关键进程全部存活：{', '.join(expected)}"))

        # ---------- 端口 ----------
        elif ctype == "port":
            raw = sections.get("port", "")
            listening = raw
            expected_ports = [int(p) for p in (check.get("ports") or [])]
            missing = [p for p in expected_ports if f":{p}" not in listening]
            if missing:
                results.append(_mk(name, ctype, default_level, f"缺失 {len(missing)} 个",
                                   f"以下端口未监听：{', '.join(str(p) for p in missing)}"))
            else:
                results.append(_mk(name, ctype, LEVEL_OK, f"{len(expected_ports)}/{len(expected_ports)} 监听",
                                   f"端口全部正常监听：{', '.join(str(p) for p in expected_ports)}"))

        # ---------- 日志 ----------
        elif ctype == "log":
            raw = sections.get("log", "")
            total = 0
            detail = []
            for line in raw.splitlines():
                if ":" not in line:
                    continue
                path, count = line.rsplit(":", 1)
                try:
                    n = int(count.strip())
                except ValueError:
                    continue
                total += n
                if n > 0:
                    detail.append(f"{path}({n})")
            if total > 0:
                results.append(_mk(name, ctype, default_level, f"{total} 条",
                                   f"最近 300 行日志中命中错误关键字 {total} 条：{'、'.join(detail)}"))
            else:
                results.append(_mk(name, ctype, LEVEL_OK, "0 条", "最近日志未发现错误关键字"))

    return results


def _mk(name: str, ctype: str, level: str, value: str, message: str) -> dict[str, Any]:
    return {"item_type": ctype, "item_name": name, "level": level, "value": value, "message": message}


def inspect_one(target: Target, checks: list[dict[str, Any]], timeout: int = 30) -> dict[str, Any]:
    """对单台目标执行一次巡检，返回 {asset, target, results, ssh}。"""
    script = build_script(checks)
    started = time.perf_counter()
    ssh_result = ssh_client.exec_command(target, script, timeout=timeout)
    elapsed = int((time.perf_counter() - started) * 1000)

    # 采集脚本"部分成功"也要用：只要拿回了分节数据就照常判级，
    # 退出码非 0 只在完全没数据时才当失败（否则一个可选文件不存在就会毁掉整次巡检）
    sections = _parse_sections(ssh_result.stdout)
    if not ssh_result.ok and not sections:
        return {
            "target": target,
            "ssh_ok": False,
            "error": ssh_result.error or f"退出码 {ssh_result.exit_code}",
            "duration_ms": elapsed,
            "results": [
                _mk("连通性", "ssh", LEVEL_ERROR, "不可达",
                    f"无法通过 SSH 完成巡检采集：{ssh_result.error or ssh_result.stderr[:200] or f'退出码 {ssh_result.exit_code}'}")
            ],
        }

    return {
        "target": target,
        "ssh_ok": True,
        "error": ssh_result.error,
        "duration_ms": elapsed,
        "results": _evaluate(checks, sections),
    }


def parse_checks(checks_json: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(checks_json or "[]")
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        log.warning("巡检模板 checks_json 解析失败，回退内置检查项")
        return BUILTIN_CHECKS


def summarize(results: list[dict[str, Any]]) -> dict[str, int]:
    """统计各等级的检查项数量，用于报告头与首页看板。"""
    stat = {LEVEL_OK: 0, LEVEL_WARN: 0, LEVEL_CRITICAL: 0, LEVEL_ERROR: 0}
    for item in results:
        stat[item.get("level", LEVEL_OK)] = stat.get(item.get("level", LEVEL_OK), 0) + 1
    return stat
