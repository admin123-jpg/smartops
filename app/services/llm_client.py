"""大模型调用层 —— 平台的「AI 智能诊断」能力。

三个使用场景：
  1. 告警根因诊断：Alertmanager 告警上下文 -> 根因 + 处置建议
  2. 巡检报告总结：一次巡检的异常项清单 -> 整体结论 + 优先处理项
  3. 日志分析：一段日志 -> 根因 + 处置命令

设计要点（面试常问）：
- **兼容多家模型**：DeepSeek / 通义 / OpenAI 都提供 OpenAI 风格的
  `/chat/completions` 接口，所以只用一套请求格式，靠 base_url + model 切换。
- **API Key 从配置读**：settings.LLM_API_KEY（.env 里的 LLM_API_KEY=sk-xxx）。
- **JSON 容错解析**：模型经常不老实返回纯 JSON（会包 ```json 代码块、加解释文字），
  所以分三级解析：直接 loads -> 剥代码块 -> 正则抽取第一个 {...}。
- **降级不中断**：没有 Key、超时、解析失败时，自动退回内置规则库给结论，
  并用 mode 字段（llm / rule）标明这条结论的来源，前端可见。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from app.config import settings

log = logging.getLogger(__name__)


# ============================================================ 底层调用
def is_configured() -> bool:
    """是否配置了 API Key。前端据此提示「当前为规则库模式」。"""
    return bool(settings.LLM_API_KEY.strip())


def chat(messages: list[dict[str, str]], temperature: float = 0.2) -> str:
    """调用 OpenAI 兼容的 /chat/completions，返回模型文本。失败抛异常，由上层降级。"""
    url = settings.LLM_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.LLM_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": settings.LLM_MAX_TOKENS,
        "stream": False,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=settings.LLM_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"模型接口返回 {resp.status_code}：{resp.text[:200]}")
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"模型返回结构异常：{str(data)[:200]}")
    return choices[0].get("message", {}).get("content", "") or ""


def parse_json(text: str) -> dict[str, Any]:
    """三级 JSON 容错解析：直接解析 -> 剥 ```json 代码块 -> 正则抽第一个 {...}。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("模型返回为空")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    block = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if block:
        try:
            return json.loads(block.group(1).strip())
        except json.JSONDecodeError:
            pass

    brace = re.search(r"\{.*\}", text, re.S)
    if brace:
        try:
            return json.loads(brace.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError("模型输出不是合法 JSON")


# ============================================================ 提示词
_SYS_DIAGNOSE = (
    "你是一名资深 Linux 运维工程师。你的任务是基于『给定事实』判断故障根因并给出处置建议。"
    "严格要求：\n"
    "1. 只依据提供的证据推理，不要臆造主机、路径、命令或日志内容；证据不足时明确说「证据不足」。\n"
    "2. 处置建议必须是可在 Linux 上直接执行的具体步骤，按优先级排序。\n"
    "3. 若建议涉及删除、重启、改配置等风险操作，必须提示影响面与回滚方式。\n"
    '4. 只输出 JSON，不要输出任何解释文字，格式：\n'
    '{"root_cause":"根因（一句话）","confidence":"high|medium|low",'
    '"evidence":["支撑证据1","支撑证据2"],'
    '"suggestions":[{"step":1,"action":"具体操作","risk":"low|medium|high"}],'
    '"summary":"给值班同事的一句话总结"}'
)


def _user_prompt_alert(ctx: dict[str, Any]) -> str:
    return (
        "【告警事实】\n"
        f"- 主机：{ctx.get('hostname', '未知')}\n"
        f"- 告警名称：{ctx.get('alert_name', '')}\n"
        f"- 指标：{ctx.get('metric', '')}\n"
        f"- 级别：{ctx.get('severity', '')}\n"
        f"- 摘要：{ctx.get('summary', '')}\n"
        f"- 描述：{ctx.get('description', '')}\n"
        f"- 首次发生：{ctx.get('first_seen_at', '')}\n"
        f"- 已重复次数：{ctx.get('repeat_count', 1)}\n"
        f"- 标签：{json.dumps(ctx.get('labels', {}), ensure_ascii=False)}\n"
        f"\n【该主机近期同类告警】\n{ctx.get('recent_alerts_text', '（无）')}\n"
        f"\n【该主机最近一次巡检异常项】\n{ctx.get('inspection_text', '（无）')}\n"
    )


def _user_prompt_inspection(ctx: dict[str, Any]) -> str:
    return (
        "【巡检事实】\n"
        f"- 巡检模板：{ctx.get('template_name', '')}\n"
        f"- 巡检主机数：{ctx.get('asset_count', 0)}\n"
        f"- 检查项统计：正常 {ctx.get('ok_items', 0)} / 警告 {ctx.get('warn_items', 0)} / 严重 {ctx.get('critical_items', 0)}\n"
        f"\n【异常项清单】\n{ctx.get('abnormal_text', '（无异常）')}\n"
        "\n请输出 JSON：{\"root_cause\":\"整体结论\",\"confidence\":\"high|medium|low\","
        "\"evidence\":[\"依据\"],\"suggestions\":[{\"step\":1,\"action\":\"操作\",\"risk\":\"low|medium|high\"}],"
        "\"summary\":\"一句话总结\"}"
    )


def _user_prompt_log(ctx: dict[str, Any]) -> str:
    return (
        "【日志事实】\n"
        f"- 来源：{ctx.get('source', '')}\n"
        f"- 命中的异常关键字：{', '.join(ctx.get('hit_keywords', [])) or '（无）'}\n"
        f"- 错误行数：{ctx.get('error_lines', 0)}\n"
        f"\n【日志片段】\n{(ctx.get('log_excerpt') or '')[:4000]}\n"
        "\n请输出 JSON：{\"root_cause\":\"根因\",\"confidence\":\"high|medium|low\","
        "\"evidence\":[\"证据\"],\"suggestions\":[{\"step\":1,\"action\":\"操作\",\"risk\":\"low|medium|high\"}],"
        "\"summary\":\"一句话总结\"}"
    )


# ============================================================ 规则库降级
_RULES: list[tuple[str, str, list[str]]] = [
    ("disk", "磁盘空间不足", [
        "du -sh /* 2>/dev/null | sort -rh | head -10   # 定位大目录",
        "find /var/log -type f -size +200M -exec ls -lh {} \\;   # 查大日志文件",
        "清理或转储大文件后再次确认 df -h",
    ]),
    ("memory", "内存不足或存在内存泄漏", [
        "free -m && ps aux --sort=-%mem | head -10",
        "检查是否有进程常驻内存持续增长",
        "必要时平滑重启占用最高的应用（注意业务窗口）",
    ]),
    ("cpu", "CPU 负载过高", [
        "uptime && top -bn1 | head -20",
        "定位高 CPU 进程：pidstat -u 1 3",
        "确认是业务量上涨还是异常进程",
    ]),
    ("port", "服务端口未监听", [
        "ss -lntp | grep <端口>",
        "systemctl status <服务名>",
        "查应用日志确认启动失败原因后重启服务",
    ]),
    ("process", "关键进程不存在", [
        "ps -ef | grep <进程名>",
        "systemctl status <服务名> 查看退出原因",
        "确认配置文件与磁盘空间无异常后拉起",
    ]),
    ("log", "日志中出现错误关键字", [
        "grep -n -i 'error\\|exception' <日志文件> | tail -50",
        "结合事发时间点前后 5 分钟上下文定位",
        "必要时回滚最近一次变更",
    ]),
    ("down", "目标不可达或服务已退出", [
        "ping -c 3 <主机> 确认网络层是否可达",
        "systemctl status <服务名> 查看退出码与最后日志",
        "确认磁盘未满、内存未耗尽后重新拉起服务",
    ]),
    ("crash", "进程反复重启", [
        "systemctl status <服务名> 查看重启次数与退出码",
        "journalctl -u <服务名> -n 200 --no-pager 定位崩溃点",
        "检查是否 OOM（dmesg | grep -i oom）或依赖不可用",
    ]),
    ("time", "节点时钟偏移", [
        "timedatectl status 查看同步状态与偏移量",
        "systemctl restart chronyd && chronyc sources -v",
        "若 NTP 源不可达，检查网络与上层防火墙放行",
    ]),
]


def rule_fallback(ctx: dict[str, Any], kind: str, focus: str = "") -> dict[str, Any]:
    """无 API Key 或调用失败时的兜底：按关键字命��规则库给建议。

    匹配顺序很重要：**先只看本条告警自身的字段（focus），匹配不上再扫完整上下文**。
    踩过的坑：一开始直接扫整个上下文，同主机的其它告警会「串味」—— 一个
    KubeletDown 告警因为上下文里提到了磁盘告警，被误判成「磁盘空间不足」。
    """
    primary = (focus or "").lower()
    blob = json.dumps(ctx, ensure_ascii=False).lower()

    hit_name, hits = None, []
    for key, name, steps in _RULES:
        if key in primary or name in primary:
            hit_name, hits = name, steps
            break
    if not hit_name:
        for key, name, steps in _RULES:
            if key in blob or name in blob:
                hit_name, hits = name, steps
                break
    if not hit_name:
        hit_name, hits = "未匹配到明确规则", [
            "先确认 service 状态与最近变更：systemctl status <服务名>",
            "查看对应日志文件最后 100 行",
            "如仍无法定位，交由人工介入",
        ]
    return {
        "mode": "rule",
        "root_cause": f"规则库判断：{hit_name}",
        "confidence": "low",
        "evidence": [f"上下文命中规则关键词：{hit_name}"],
        "suggestions": [{"step": i + 1, "action": a, "risk": "medium"} for i, a in enumerate(hits)],
        "summary": f"未接入模型，按内置规则给出「{hit_name}」的排查路径（{kind}）",
    }


# ============================================================ 对外能力
def _focus_of(ctx: dict[str, Any], kind: str) -> str:
    """提取「本条问题自身的特征词」，供规则库优先匹配，避免被上下文串味。"""
    if kind == "alert":
        parts = [ctx.get("alert_name", ""), ctx.get("metric", ""),
                 ctx.get("summary", ""), ctx.get("description", "")]
        return " ".join(str(p) for p in parts if p)
    if kind == "log":
        return " ".join(ctx.get("hit_keywords", []) or [])
    return str(ctx.get("template_name", ""))


def _diagnose(ctx: dict[str, Any], kind: str) -> dict[str, Any]:
    focus = _focus_of(ctx, kind)
    if not is_configured():
        result = rule_fallback(ctx, kind, focus)
        result["error"] = "未配置 LLM_API_KEY，已降级为规则库"
        return result
    try:
        prompt = {
            "alert": _user_prompt_alert,
            "inspection": _user_prompt_inspection,
            "log": _user_prompt_log,
        }[kind](ctx)
        raw = chat([
            {"role": "system", "content": _SYS_DIAGNOSE},
            {"role": "user", "content": prompt},
        ])
        parsed = parse_json(raw)
        parsed.setdefault("root_cause", "")
        parsed.setdefault("confidence", "medium")
        parsed.setdefault("evidence", [])
        parsed.setdefault("suggestions", [])
        parsed.setdefault("summary", "")
        parsed["mode"] = "llm"
        parsed["model"] = settings.LLM_MODEL
        return parsed
    except Exception as exc:  # noqa: BLE001
        log.warning("大模型诊断失败，降级规则库：%s", exc)
        result = rule_fallback(ctx, kind, focus)
        result["error"] = f"模型调用失败：{exc}"
        return result


def diagnose_alert(ctx: dict[str, Any]) -> dict[str, Any]:
    return _diagnose(ctx, "alert")


def summarize_inspection(ctx: dict[str, Any]) -> dict[str, Any]:
    return _diagnose(ctx, "inspection")


def analyze_log(ctx: dict[str, Any]) -> dict[str, Any]:
    return _diagnose(ctx, "log")


def render_suggestions(result: dict[str, Any]) -> str:
    """把建议列表渲染成多行文本，便于写库和在页面上直接展示。"""
    lines = []
    for item in result.get("suggestions", []):
        if isinstance(item, dict):
            risk = item.get("risk", "")
            flag = f" [风险:{risk}]" if risk and risk != "low" else ""
            lines.append(f"{item.get('step', '')}. {item.get('action', '')}{flag}")
        else:
            lines.append(str(item))
    return "\n".join(lines)
