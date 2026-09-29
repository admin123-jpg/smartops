# -*- coding: utf-8 -*-
"""端到端冒烟测试：登录 -> 录入资产 -> 探测 -> 批量执行 -> 巡检 -> 告警 -> 日志。

跑法：先启动服务，再执行本脚本。输出写到 scripts/smoke_result.txt。
"""
from __future__ import annotations

import json
import time

import requests

from _env import (APP_VIEWER, APP_VIEWER_PASSWORD, BASE, APP_PASSWORD, APP_USER,
                  SSH_PASSWORD, SSH_PORT, SSH_USER, require_targets)

USER, PWD = APP_USER, APP_PASSWORD
VM_IP = require_targets()[0]

out: list[str] = []


def log(*a) -> None:
    line = " ".join(str(x) for x in a)
    out.append(line)
    print(line)


def dump(title: str, obj, limit: int = 900) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    log(f"\n----- {title} -----")
    log(text[:limit])


def main() -> None:
    s = requests.Session()

    # 0) 健康检查
    r = s.get(f"{BASE}/api/health", timeout=10).json()
    dump("健康检查", r)

    # 1) 登录
    r = s.post(f"{BASE}/api/auth/login", json={"username": USER, "password": PWD}, timeout=10).json()
    assert r["code"] == 0, r
    token = r["data"]["access_token"]
    s.headers["Authorization"] = f"Bearer {token}"
    log(f"\n[1] 登录成功：{r['data']['username']} / {r['data']['role']}")

    # 2) 权限校验：只读用户不该能建资产
    ro = requests.post(f"{BASE}/api/auth/login",
                       json={"username": APP_VIEWER, "password": APP_VIEWER_PASSWORD},
                       timeout=10).json()
    ro_t = ro["data"]["access_token"]
    rr = requests.post(f"{BASE}/api/assets", headers={"Authorization": f"Bearer {ro_t}"},
                       json={"ip": "10.0.0.99"}, timeout=10)
    log(f"[2] 只读角色写操作 -> HTTP {rr.status_code}（预期 403）")

    # 3) 录入资产
    r = s.post(f"{BASE}/api/assets", json={
        "ip": VM_IP, "ssh_port": SSH_PORT, "ssh_user": SSH_USER, "password": SSH_PASSWORD,
        "auth_type": "password", "env": "prod", "purpose": "被管服务器",
    }, timeout=15).json()
    if r.get("code") != 0:
        # 已存在（或其它业务错误）则查出来复用
        lst = s.get(f"{BASE}/api/assets", params={"keyword": VM_IP}, timeout=10).json()
        assert lst["data"]["items"], f"录入资产失败：{r}"
        asset = lst["data"]["items"][0]
        log(f"[3] 资产已存在，复用 id={asset['id']}（{r.get('detail') or r.get('message')}）")
    else:
        asset = r["data"]
        log(f"[3] 资产录入成功 id={asset['id']} ip={asset['ip']}")
    aid = asset["id"]

    # 4) SSH 探测
    r = s.post(f"{BASE}/api/assets/{aid}/probe", timeout=60).json()
    dump("SSH 探测结果", r)
    log(f"[4] 探测 {'成功' if r['data']['ok'] else '失败'}，耗时 {r['data']['duration_ms']}ms")

    # 5) 批量执行（eager 模式下同步完成）
    r = s.post(f"{BASE}/api/executor/tasks", json={
        "asset_ids": [aid], "command": "hostname; uptime; df -h / | tail -1",
    }, timeout=120).json()
    dump("批量执行提交", r)
    task_id = r["data"]["task_id"]
    time.sleep(1)
    d = s.get(f"{BASE}/api/executor/tasks/{task_id}", timeout=20).json()
    dump("批量执行结果", d, limit=1500)

    # 6) 高危命令拦截
    r = s.post(f"{BASE}/api/executor/tasks", json={
        "asset_ids": [aid], "command": "rm -rf /tmp/test",
    }, timeout=20)
    log(f"\n[6] 高危命令未确认 -> HTTP {r.status_code}（预期 428）：{r.json().get('message', '')[:60]}")

    # 7) 巡检
    r = s.post(f"{BASE}/api/inspection/runs", json={"asset_ids": [aid]}, timeout=30).json()
    dump("发起巡检", r)
    time.sleep(3)
    run_detail = s.get(f"{BASE}/api/inspection/runs/{r['data']['run_id']}", timeout=30).json()
    d = run_detail["data"]
    log(f"\n[7] 巡检完成：检查项 {d['total_items']} 项，正常 {d['ok_items']} / 警告 {d['warn_items']} / 严重 {d['critical_items']}")
    log(f"    报告路径：{d['report_path']}")
    for item in d["results"][:12]:
        log(f"    [{item['level']:8}] {item['item_name']:12} | {item['value']:18} | {item['message'][:60]}")
    if d.get("ai_summary"):
        log("    AI 结论：" + d["ai_summary"][:300].replace("\n", " / "))

    # 8) 告警注入 + 去重 + AI 诊断
    r = s.post(f"{BASE}/api/alerts/mock", params={"count": 4}, timeout=90).json()
    dump("模拟告警注入", r)
    time.sleep(1)
    lst = s.get(f"{BASE}/api/alerts", params={"page_size": 5}, timeout=20).json()
    for a in lst["data"]["items"][:4]:
        log(f"    {a['hostname']:8} {a['alert_name']:28} {a['severity']:8} 重复{a['repeat_count']} | 根因：{(a['ai_root_cause'] or '')[:40]} | 来源:{a['ai_mode']}")
    st = s.get(f"{BASE}/api/alerts/stats", timeout=20).json()
    dump("告警看板", st)

    # 9) 日志分析（示例 Nginx 日志 + AI 诊断）
    demo = s.get(f"{BASE}/api/logs/demo-samples", timeout=20).json()["data"]
    r = s.post(f"{BASE}/api/logs/inline", json={
        "text": demo["text"], "slow_threshold": 1.0, "diagnose": True, "source": demo["source"],
    }, timeout=120).json()
    a = r["data"]["analysis"]
    log(f"\n[9] 日志分析：格式={a['format']} 解析行数={a['matched_lines']} 慢请求={len(a.get('slow_requests', []))}")
    log(f"    Top IP：{a.get('top_ips', [])[:3]}")
    log(f"    状态码分布：{a.get('status_distribution')}")
    if r["data"].get("ai"):
        log(f"    AI 根因：{r['data']['ai'].get('root_cause', '')[:120]}（来源 {r['data']['ai'].get('mode')}）")

    # 10) 看板
    ov = s.get(f"{BASE}/api/dashboard/overview", timeout=20).json()["data"]
    log(f"\n[10] 看板：资产 {ov['assets']} / 任务 {ov['tasks']} / 告警未恢复 {ov['alerts']['firing']} / AI 模式 {ov['llm']['mode']}")
    log(f"     Redis 可用={ov['system']['redis_available']}  执行模式={ov['system']['async_mode']}")

    # 11) 审计
    au = s.get(f"{BASE}/api/auth/audit", params={"page_size": 5}, timeout=20).json()["data"]
    log(f"\n[11] 审计日志最近 {len(au['items'])} 条：" + ", ".join(f"{x['action']}({x['operator']})" for x in au["items"]))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"\n!!! 测试中断：{type(exc).__name__}: {exc}")
    open(r"E:\smartops\scripts\smoke_result.txt", "w", encoding="utf-8").write("\n".join(out))
