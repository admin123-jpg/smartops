# -*- coding: utf-8 -*-
"""真机验证：把三台 K8s 节点全部纳管，跑批量执行 + 全量巡检。

输出写入 scripts/demo3_result.txt
"""
from __future__ import annotations

import json
import time

import requests

from _env import (APP_PASSWORD, APP_USER, BASE, SSH_PASSWORD, SSH_PORT,
                  SSH_USER, require_targets)

# 目标机从环境变量读（见 scripts/_env.py）—— 地址和凭据不写死在代码里
NODES = [(ip, "", "prod", "被管节点") for ip in require_targets()]

out: list[str] = []


def log(*a):
    out.append(" ".join(str(x) for x in a))


def main():
    s = requests.Session()
    t = s.post(f"{BASE}/api/auth/login",
               json={"username": APP_USER, "password": APP_PASSWORD}, timeout=10).json()
    s.headers["Authorization"] = "Bearer " + t["data"]["access_token"]
    log("登录成功")

    # 1) 纳管
    for ip, host, env, purpose in NODES:
        r = s.post(f"{BASE}/api/assets", json={
            "ip": ip, "ssh_port": SSH_PORT, "ssh_user": SSH_USER, "password": SSH_PASSWORD,
            "auth_type": "password", "env": env, "purpose": purpose, "hostname": host,
        }, timeout=15).json()
        log(f"纳管 {ip:18} -> {'新建' if r.get('code') == 0 else '已存在'}")

    # 2) 批量探测
    r = s.post(f"{BASE}/api/assets/probe/batch", timeout=180).json()
    log(f"\n批量探测：共 {r['data']['total']} 台，在线 {r['data']['online']} 台")
    for it in r["data"]["items"]:
        log(f"  {it['ip']:18} {it['status']:8} {it.get('error', '')}")

    lst = s.get(f"{BASE}/api/assets?page_size=50", timeout=20).json()["data"]["items"]
    log("\n资产清单（探测后的真实配置）：")
    for a in lst:
        log(f"  {a['hostname'] or '-':8} {a['ip']:18} {a['os_version'][:34]:36} {a['cpu_cores']}核/{a['memory_mb']}MB "
            f"磁盘{a['disk_used_percent']}% 内核{a['kernel'][:22]}")

    # 3) 批量执行（三台一起跑）
    ids = [a["id"] for a in lst]
    cmd = ("hostname; uptime | sed 's/.*load average/Load average/'; "
           "df -h / | tail -1 | awk '{print \"root 使用率 \" $5}'")
    r = s.post(f"{BASE}/api/executor/tasks", json={"asset_ids": ids, "command": cmd, "name": "三节点状态巡检"}, timeout=180).json()
    task_id = r["data"]["task_id"]
    log(f"\n批量执行已提交 task_id={task_id}，目标 {r['data']['target_count']} 台")
    # 轮询到任务结束（最多 60 秒）。注意：接口在上面就已经返回了，
    # 这里是我们自己主动来查结果的 —— 正好体现"Web 立刻返回、任务在后台跑"。
    for _ in range(60):
        time.sleep(1)
        d = s.get(f"{BASE}/api/executor/tasks/{task_id}", timeout=30).json()["data"]
        if d["status"] not in ("pending", "running"):
            break
    log(f"执行结果：状态={d['status']} 成功={d['success_count']} 失败={d['failed_count']}")
    for res in d["results"]:
        log(f"  --- {res['asset_label']} 退出码={res['exit_code']} 耗时={res['duration_ms']}ms ---")
        for line in (res["stdout"] or "").strip().splitlines():
            log(f"      {line}")
        if res["stderr"]:
            log("      [stderr] " + res["stderr"].strip()[:200])

    # 4) 全量巡检
    r = s.post(f"{BASE}/api/inspection/runs", json={"asset_ids": ids}, timeout=30).json()
    run_id = r["data"]["run_id"]
    log(f"\n全量巡检已发起 run_id={run_id}，目标 {r['data']['target_count']} 台")
    for _ in range(40):
        time.sleep(2)
        d = s.get(f"{BASE}/api/inspection/runs/{run_id}", timeout=30).json()["data"]
        if d["status"] == "finished":
            break
    log(f"巡检完成：检查项 {d['total_items']} 项，正常 {d['ok_items']} / 警告 {d['warn_items']} / 严重 {d['critical_items']}")
    log(f"HTML 报告：{d['report_path']}")
    log("\n逐项结果（只列异常 / 或各主机首项）：")
    for it in d["results"]:
        if it["level"] != "ok" or True:
            log(f"  [{it['level']:8}] {it['asset_label']:24} {it['item_name']:12} | {it['value']:14} | {it['message'][:80]}")
    if d.get("ai_summary"):
        log("\n===== AI 巡检结论 =====")
        log(d["ai_summary"])

    # 5) 看板
    ov = s.get(f"{BASE}/api/dashboard/overview", timeout=20).json()["data"]
    log("\n===== 总览看板 =====")
    log(json.dumps({"assets": ov["assets"], "tasks": ov["tasks"],
                    "alerts": {k: ov["alerts"][k] for k in ("today_total", "firing", "diagnose_rate")},
                    "top_disk": ov["top_disk"], "system": ov["system"], "llm": ov["llm"]},
                   ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        log(f"\n!!! 中断：{type(exc).__name__}: {exc}")
    open(r"E:\smartops\scripts\demo3_result.txt", "w", encoding="utf-8").write("\n".join(out))
