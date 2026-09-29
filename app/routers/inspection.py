"""自动化巡检接口（M3）。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.asset import Asset
from app.models.inspection import InspectionResult, InspectionRun, InspectionTemplate
from app.schemas.inspection import (
    InspectionResultOut,
    InspectionRunCreate,
    InspectionRunOut,
    InspectionTemplateCreate,
    InspectionTemplateOut,
)
from app.services import inspector
from app.tasks import inspection as inspection_task
from app.utils.response import ok
from app.utils.security import CurrentUser, RequireOps

router = APIRouter(prefix="/api/inspection", tags=["自动化巡检"])


def ensure_builtin_template(db: Session) -> InspectionTemplate:
    """首次启动时把内置检查项落库，保证开箱即用。"""
    tpl = db.query(InspectionTemplate).filter(InspectionTemplate.is_builtin == 1).first()
    if tpl:
        return tpl
    tpl = InspectionTemplate(
        name="默认巡检模板",
        description="磁盘 / 内存 / 负载 / 关键进程 / 端口监听 / 系统日志错误，六类基础检查",
        checks_json=json.dumps(inspector.BUILTIN_CHECKS, ensure_ascii=False),
        is_builtin=1,
    )
    db.add(tpl)
    db.commit()
    return tpl


@router.get("/templates", summary="巡检模板列表")
def list_templates(_: CurrentUser, db: Session = Depends(get_db)) -> dict:
    ensure_builtin_template(db)
    rows = db.query(InspectionTemplate).order_by(InspectionTemplate.id).all()
    return ok([
        {
            "id": t.id, "name": t.name, "description": t.description,
            "is_builtin": t.is_builtin,
            "checks": json.loads(t.checks_json or "[]"),
        }
        for t in rows
    ])


@router.post("/templates", summary="新建巡检模板")
def create_template(payload: InspectionTemplateCreate, _: RequireOps,
                    db: Session = Depends(get_db)) -> dict:
    if db.query(InspectionTemplate).filter(InspectionTemplate.name == payload.name).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "模板名已存在")
    tpl = InspectionTemplate(
        name=payload.name,
        description=payload.description,
        checks_json=json.dumps(payload.checks, ensure_ascii=False),
    )
    db.add(tpl)
    db.commit()
    return ok(InspectionTemplateOut.model_validate(tpl).model_dump())


@router.post("/runs", summary="发起巡检（一键 / 定时）")
def create_run(payload: InspectionRunCreate, user: RequireOps,
               db: Session = Depends(get_db)) -> dict:
    tpl = db.get(InspectionTemplate, payload.template_id) if payload.template_id else \
        ensure_builtin_template(db)
    if not tpl:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "巡检模板不存在")

    query = db.query(Asset)
    if payload.asset_ids:
        query = query.filter(Asset.id.in_(payload.asset_ids))
    else:
        query = query.filter(Asset.status == "online")
    assets = query.all()
    if not assets:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "没有可巡检的在线资产，请先录入并探测")

    run = InspectionRun(
        template_id=tpl.id,
        template_name=tpl.name,
        asset_ids=",".join(str(a.id) for a in assets),
        status="pending",
        trigger="manual",
        operator=user.username,
    )
    db.add(run)
    db.commit()

    inspection_task.run_inspection_task.delay(run.id)
    return ok({"run_id": run.id, "template": tpl.name, "target_count": len(assets)})


@router.get("/runs", summary="巡检历史")
def list_runs(_: CurrentUser, db: Session = Depends(get_db),
              page: int = 1, page_size: int = 20) -> dict:
    total = db.query(InspectionRun).count()
    rows = (db.query(InspectionRun).order_by(InspectionRun.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return ok({"items": [InspectionRunOut.model_validate(r).model_dump() for r in rows],
               "total": total, "page": page, "page_size": page_size})


@router.get("/runs/{run_id}", summary="巡检详情（含逐项结果）")
def run_detail(run_id: int, _: CurrentUser, db: Session = Depends(get_db)) -> dict:
    run = db.get(InspectionRun, run_id)
    if not run:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "巡检批次不存在")
    rows = (db.query(InspectionResult).filter(InspectionResult.run_id == run_id)
            .order_by(InspectionResult.level.desc()).all())
    data = InspectionRunOut.model_validate(run).model_dump()
    data["results"] = [InspectionResultOut.model_validate(r).model_dump() for r in rows]
    return ok(data)


@router.get("/runs/{run_id}/report", summary="打开巡检 HTML 报告")
def run_report(run_id: int, db: Session = Depends(get_db)):
    run = db.get(InspectionRun, run_id)
    if not run or not run.report_path:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "报告尚未生成")
    return FileResponse(run.report_path, media_type="text/html")


@router.get("/runs/{run_id}/progress", summary="巡检进度快照")
def run_progress(run_id: int, _: CurrentUser) -> dict:
    from app.tasks.batch_exec import progress_snapshot

    return ok(progress_snapshot(run_id))
