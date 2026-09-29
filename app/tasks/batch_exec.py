"""批量执行任务（M2 的后台工人）。

一个任务的生命周期：
  API 创建 TaskRecord(status=pending) -> 提交 Celery -> worker 逐台执行
  -> 每台写完 TaskTargetResult 并更新进度快照 -> 前端 WebSocket 轮询快照实时刷新
  -> 全部结束汇总 status(success/partial/failed)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from app.config import settings
from app.db import SessionLocal
from app.models.asset import Asset
from app.models.task import (
    TASK_FAILED,
    TASK_PARTIAL,
    TASK_RUNNING,
    TASK_SUCCESS,
    TaskRecord,
    TaskTargetResult,
)
from app.services import ssh_client
from app.services.ssh_client import Target
from app.tasks.celery_app import celery_app
from app.utils import redis_client

log = logging.getLogger(__name__)


def to_target(asset: Asset) -> Target:
    return Target(
        asset_id=asset.id,
        ip=asset.ip,
        ssh_port=asset.ssh_port or 22,
        ssh_user=asset.ssh_user or "root",
        auth_type=asset.auth_type or "password",
        password=asset.password or "",
        private_key_path=asset.private_key_path or "",
        label=f"{asset.hostname or asset.ip}",
    )


def _publish_progress(task_id: int, payload: dict) -> None:
    """进度快照：写 Redis（WebSocket 轮询它），同时 Pub/Sub 广播一份。"""
    body = json.dumps(payload, ensure_ascii=False)
    redis_client.setex(f"task:progress:{task_id}", 600, body)
    redis_client.publish(f"task:events:{task_id}", body)


@celery_app.task(name="app.tasks.batch_exec.run_batch", bind=True)
def run_batch(self, task_id: int) -> dict:
    """执行一次批量操作。"""
    db = SessionLocal()
    try:
        task = db.get(TaskRecord, task_id)
        if not task:
            return {"ok": False, "error": f"任务 {task_id} 不存在"}

        asset_ids = [int(x) for x in (task.asset_ids or "").split(",") if x.strip().isdigit()]
        assets = db.query(Asset).filter(Asset.id.in_(asset_ids)).all() if asset_ids else []
        if not assets:
            task.status = TASK_FAILED
            task.finished_at = datetime.now()
            db.commit()
            return {"ok": False, "error": "没有找到目标资产"}

        task.status = TASK_RUNNING
        task.celery_task_id = self.request.id or ""
        db.commit()

        command = task.command or ""
        # 先给每台建一条 pending 记录，前端一打开就能看到全部目标
        rows: dict[int, TaskTargetResult] = {}
        for asset in assets:
            row = TaskTargetResult(
                task_id=task.id,
                asset_id=asset.id,
                asset_label=f"{asset.hostname or ''}({asset.ip})",
                status="pending",
            )
            db.add(row)
            rows[asset.id] = row
        db.commit()

        total = len(assets)
        done = success = failed = 0
        _publish_progress(task.id, {"task_id": task.id, "status": TASK_RUNNING,
                                    "total": total, "done": 0, "success": 0, "failed": 0,
                                    "results": []})

        def worker(target: Target) -> ssh_client.SSHResult:
            return ssh_client.exec_command(target, command)

        # 用 iter_parallel 而不是 run_parallel：谁先跑完谁先落库 + 先推进度，
        # 这样前端才是「一台一台冒出来」，而不是等整批跑完才一起出来。
        for target, result in ssh_client.iter_parallel([to_target(a) for a in assets], worker):
            row = rows.get(target.asset_id)
            if row is None:
                continue
            row.status = "success" if result.ok else "failed"
            row.exit_code = result.exit_code
            row.stdout = (result.stdout or "")[:20000]
            row.stderr = (result.stderr or "")[:8000]
            row.error = result.error or ""
            row.duration_ms = result.duration_ms
            db.commit()

            done += 1
            if result.ok:
                success += 1
            else:
                failed += 1
            _publish_progress(task.id, {
                "task_id": task.id, "status": TASK_RUNNING, "total": total,
                "done": done, "success": success, "failed": failed,
                "latest": {
                    "asset_id": target.asset_id,
                    "label": target.label or target.ip,
                    "status": row.status,
                    "exit_code": row.exit_code,
                    "stdout": row.stdout[-2000:],
                    "stderr": row.stderr[-1000:],
                    "error": row.error,
                    "duration_ms": row.duration_ms,
                },
            })

        task.success_count = success
        task.failed_count = failed
        task.status = TASK_SUCCESS if failed == 0 else (TASK_PARTIAL if success else TASK_FAILED)
        task.finished_at = datetime.now()
        db.commit()

        final = {"task_id": task.id, "status": task.status, "total": total,
                 "done": done, "success": success, "failed": failed,
                 # 带上完整逐机结果：最后一台完成和「任务结束」很可能落在同一个
                 # 500ms 轮询窗口里，只靠 latest 会把它冲掉 —— 前端拿到 results
                 # 才能保证整批结果一定完整。
                 "results": [
                     {"asset_id": r.asset_id, "label": r.asset_label, "status": r.status,
                      "exit_code": r.exit_code, "stdout": (r.stdout or "")[-2000:],
                      "stderr": (r.stderr or "")[-1000:], "error": r.error,
                      "duration_ms": r.duration_ms}
                     for r in rows.values()
                 ]}
        _publish_progress(task.id, final)
        return {"ok": True, **final}
    except Exception as exc:  # noqa: BLE001
        log.exception("批量任务 %s 执行异常", task_id)
        try:
            task = db.get(TaskRecord, task_id)
            if task:
                task.status = TASK_FAILED
                task.finished_at = datetime.now()
                db.commit()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


def submit(task_id: int) -> str:
    """提交任务到队列；eager 模式下会同步执行完再返回。"""
    async_result = run_batch.delay(task_id)
    return getattr(async_result, "id", "") or ""


def progress_snapshot(task_id: int) -> dict:
    """给 WebSocket 用的进度快照；Redis 不可用时退化为直接查库。"""
    raw = redis_client.get(f"task:progress:{task_id}")
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    db = SessionLocal()
    try:
        task = db.get(TaskRecord, task_id)
        if not task:
            return {"task_id": task_id, "status": "unknown"}
        rows = db.query(TaskTargetResult).filter(TaskTargetResult.task_id == task_id).all()
        return {
            "task_id": task_id,
            "status": task.status,
            "total": task.target_count,
            "done": sum(1 for r in rows if r.status in ("success", "failed")),
            "success": task.success_count,
            "failed": task.failed_count,
            "results": [
                {"asset_id": r.asset_id, "label": r.asset_label, "status": r.status,
                 "exit_code": r.exit_code, "stdout": r.stdout[-2000:], "stderr": r.stderr[-1000:],
                 "error": r.error, "duration_ms": r.duration_ms}
                for r in rows
            ],
        }
    finally:
        db.close()


def contains_dangerous(command: str) -> list[str]:
    """命中高危关键词的命令需要二次确认后才允许执行。"""
    lowered = (command or "").lower()
    return [kw for kw in settings.DANGEROUS_KEYWORDS if kw.lower() in lowered]
