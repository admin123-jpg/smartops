"""批量执行接口（M2 核心）。

同步部分：创建任务、查历史、看单次结果。
异步部分：WebSocket 把 worker 的执行进度实时推给前端 ——
这是「选 50 台机器下发命令、每台结果实时刷出来」的关键。
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_db
from app.models.asset import Asset
from app.models.task import TaskRecord, TaskTargetResult
from app.schemas.task import TaskCreate, TaskDetailOut, TaskOut, TaskTargetResultOut
from app.tasks import batch_exec
from app.utils.response import ok
from app.utils.security import CurrentUser, RequireOps, decode_token

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/executor", tags=["批量执行"])

TERMINAL = {"success", "partial", "failed"}


@router.post("/tasks", summary="创建批量执行任务")
def create_task(payload: TaskCreate, user: RequireOps, db: Session = Depends(get_db)) -> dict:
    assets = db.query(Asset).filter(Asset.id.in_(payload.asset_ids)).all()
    if not assets:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "目标资产不存在")

    dangerous = batch_exec.contains_dangerous(payload.command)
    if dangerous and not payload.confirmed:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            f"命令命中高危关键词 {dangerous}，请确认后重试（confirmed=true）",
        )

    task = TaskRecord(
        name=payload.name or f"批量执行（{len(assets)} 台）",
        task_type=payload.task_type,
        command=payload.command,
        asset_ids=",".join(str(a.id) for a in assets),
        target_count=len(assets),
        status="pending",
        need_confirm=1 if dangerous else 0,
        confirmed_by=user.username if dangerous else "",
        operator=user.username,
    )
    db.add(task)
    db.commit()

    task.celery_task_id = batch_exec.submit(task.id)
    db.refresh(task)
    return ok({"task_id": task.id, "status": task.status, "target_count": task.target_count,
               "dangerous": dangerous,
               "ws_url": f"/api/executor/ws/{task.id}"})


@router.get("/tasks", summary="执行历史")
def list_tasks(_: CurrentUser, db: Session = Depends(get_db),
               page: int = 1, page_size: int = 20, status_filter: str = "") -> dict:
    query = db.query(TaskRecord)
    if status_filter:
        query = query.filter(TaskRecord.status == status_filter)
    total = query.count()
    rows = query.order_by(TaskRecord.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({"items": [TaskOut.model_validate(t).model_dump() for t in rows],
               "total": total, "page": page, "page_size": page_size})


@router.get("/tasks/{task_id}", summary="任务详情（含逐机结果）")
def task_detail(task_id: int, _: CurrentUser, db: Session = Depends(get_db)) -> dict:
    task = db.get(TaskRecord, task_id)
    if not task:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    rows = db.query(TaskTargetResult).filter(TaskTargetResult.task_id == task_id).all()
    detail = TaskDetailOut.model_validate(task)
    detail.results = [TaskTargetResultOut.model_validate(r) for r in rows]
    return ok(detail.model_dump())


@router.get("/tasks/{task_id}/progress", summary="任务进度快照（HTTP 轮询兜底）")
def task_progress(task_id: int, _: CurrentUser) -> dict:
    return ok(batch_exec.progress_snapshot(task_id))


@router.websocket("/ws/{task_id}")
async def task_ws(websocket: WebSocket, task_id: int, token: str = "") -> None:
    """WebSocket：实时推送任务进度。

    鉴权走 ?token=（浏览器 WebSocket API 不能自定义 Header）。
    实现上是「服务端每 500ms 取一次进度快照」而非订阅 Redis Pub/Sub ——
    这样 Redis 挂了也能从数据库读到真实状态，前端体验不受影响。
    """
    try:
        payload = decode_token(token) if token else None
    except Exception:  # noqa: BLE001
        payload = None
    if not payload:
        await websocket.close(code=4401)
        return

    await websocket.accept()
    last_body = ""
    try:
        while True:
            snapshot = batch_exec.progress_snapshot(task_id)
            body = json.dumps(snapshot, ensure_ascii=False, default=str)
            if body != last_body:
                await websocket.send_text(body)
                last_body = body
            if snapshot.get("status") in TERMINAL:
                await websocket.send_text(json.dumps({"event": "done", **snapshot}, ensure_ascii=False, default=str))
                break
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        log.info("客户端断开 WebSocket：task=%s", task_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("WebSocket 异常：%s", exc)
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@router.post("/tasks/{task_id}/rerun", summary="按原参数重跑一次")
def rerun(task_id: int, user: RequireOps, db: Session = Depends(get_db)) -> dict:
    old = db.get(TaskRecord, task_id)
    if not old:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    new_task = TaskRecord(
        name=f"{old.name}（重跑）",
        task_type=old.task_type,
        command=old.command,
        asset_ids=old.asset_ids,
        target_count=old.target_count,
        status="pending",
        operator=user.username,
    )
    db.add(new_task)
    db.commit()
    new_task.celery_task_id = batch_exec.submit(new_task.id)
    db.commit()
    return ok({"task_id": new_task.id})


def _db() -> Session:  # pragma: no cover - 便于脚本调试
    return SessionLocal()
