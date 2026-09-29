"""验证 Celery 异步执行 + WebSocket 逐台刷新。

验证三点：
  1. POST 创建任务后**立刻返回**（不阻塞等 3 台机器跑完）
  2. 每台机器的结果**陆续**通过 WebSocket 推过来（不是最后一次性给）
  3. 任务最终状态正确

三台机器故意给不同耗时（2s / 4s / 6s），这样"逐台刷新"才看得出来。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import requests
import websockets

# 默认连直连应用的 8100；想验证 Nginx 转发就设 SMARTOPS_BASE=http://localhost
BASE = os.getenv("SMARTOPS_BASE", "http://127.0.0.1:8100")
WS_BASE = BASE.replace("https://", "wss://").replace("http://", "ws://")
OUT = Path(__file__).with_name("async_demo_result.txt")

# 三台机器故意不同耗时，方便观察"一台一台推过来"
CMD = ('H=$(hostname); case $H in master) D=2;; node1) D=4;; *) D=6;; esac; '
       'echo "$H 开始执行，预计 ${D}s"; sleep $D; echo "$H 执行完成"; uptime')

lines: list[str] = []


def log(msg: str) -> None:
    print(msg)
    lines.append(msg)


def login() -> str:
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": "admin", "password": "admin123"}, timeout=10).json()
    assert r["code"] == 0, r
    return r["data"]["access_token"]


async def main() -> None:
    token = login()
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"

    assets = s.get(f"{BASE}/api/assets", params={"page_size": 50}, timeout=10).json()["data"]["items"]
    ids = [a["id"] for a in assets]
    log(f"[0] 纳管资产 {len(ids)} 台：" + "、".join(f"{a['hostname']}({a['ip']})" for a in assets))
    assert ids, "没有资产可执行"

    host = s.get(f"{BASE}/api/health", timeout=5).json()["data"]
    log(f"[1] 队列状态：broker_available={host['broker_available']}  eager_mode={host['eager_mode']}")

    # ---------- 关键点：POST 应该立刻返回 ----------
    t_post = time.perf_counter()
    resp = s.post(f"{BASE}/api/executor/tasks", json={
        "name": "异步验证：三台不同耗时",
        "task_type": "exec",
        "command": CMD,
        "asset_ids": ids,
    }, timeout=20).json()
    post_ms = (time.perf_counter() - t_post) * 1000
    assert resp["code"] == 0, resp
    task_id = resp["data"]["task_id"]
    log(f"[2] 创建任务 task_id={task_id}，POST 返回耗时 {post_ms:.0f} ms")
    log("    ↑ 这三台机器一共要跑 2+4+6 秒，但接口没有等它们 —— 这就是异步的效果")

    # ---------- 连 WebSocket 收逐台结果 ----------
    uri = f"{WS_BASE}/api/executor/ws/{task_id}?token={token}"
    pushes: list[tuple[float, dict]] = []
    t_ws = time.perf_counter()
    async with websockets.connect(uri, open_timeout=10) as ws:
        log(f"    [连接建立于 +{(time.perf_counter() - t_ws) * 1000:.0f} ms]")
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=60)
            snap = json.loads(raw)
            pushes.append(((time.perf_counter() - t_ws) * 1000, snap))
            if "latest" in snap:
                la = snap["latest"]
                log(f"    [+{pushes[-1][0]:7.0f} ms] 推送 {la['label']:22} "
                    f"exit={la['exit_code']} 耗时={la.get('duration_ms')}ms "
                    f"进度 {snap['done']}/{snap['total']}")
            elif snap.get("event") == "done" or snap.get("status") in {"success", "partial", "failed"}:
                detail_rows = snap.get("results") or []
                log(f"    [+{pushes[-1][0]:7.0f} ms] 任务结束 status={snap.get('status')} "
                    f"成功={snap.get('success')} 失败={snap.get('failed')} "
                    f"（终态携带逐机明细 {len(detail_rows)} 条）")
                for row in detail_rows:
                    log(f"          {row.get('label', '?'):22} exit={row.get('exit_code')} "
                        f"{row.get('duration_ms')}ms")
                break
            else:
                log(f"    [+{pushes[-1][0]:7.0f} ms] 状态快照 status={snap.get('status')} "
                    f"进度 {snap.get('done', 0)}/{snap.get('total', '?')}")

    total_ms = (time.perf_counter() - t_ws) * 1000
    log(f"[3] WebSocket 共收到 {len(pushes)} 条消息，最后一条在 +{total_ms:.0f} ms")

    # 用「每台机器第一条结果」的到达时间，证明是陆续推的
    log("[4] 逐台到达时间（相对连接建立）：")
    seen = set()
    for off, snap in pushes:
        la = snap.get("latest")
        if la and la["asset_id"] not in seen:
            seen.add(la["asset_id"])
            log(f"      {la['label']:22} 首次出现于 +{off:.0f} ms")

    detail = s.get(f"{BASE}/api/executor/tasks/{task_id}", timeout=10).json()["data"]
    log(f"[5] 落库结果：status={detail['status']} 目标={detail['target_count']} "
        f"成功={detail['success_count']} 失败={detail['failed_count']}")
    for r in detail["results"]:
        first = (r["stdout"] or "").strip().splitlines()
        log(f"      {r['asset_label']:22} exit={r['exit_code']} | {first[0] if first else ''}")

    log("")
    log(f"结论：POST 用 {post_ms:.0f} ms 返回，任务实际跑了 {total_ms:.0f} ms —— 相差约 "
        f"{total_ms / max(post_ms, 1):.0f} 倍。接口没有被长任务拖住。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {type(exc).__name__}: {exc}")
    OUT.write_text("\n".join(lines), encoding="utf-8")
