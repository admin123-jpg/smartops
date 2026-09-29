"""资产管理接口：录入、分组、SSH 探测、变更留痕。"""
from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.asset import Asset, AssetChangeLog, AssetGroup
from app.schemas.asset import (
    AssetCreate,
    AssetGroupCreate,
    AssetOut,
    AssetUpdate,
    ProbeResult,
)
from app.services import ssh_client
from app.services.ssh_client import Target
from app.tasks.batch_exec import to_target
from app.utils.response import ok
from app.utils.security import CurrentUser, RequireAdmin, RequireOps

router = APIRouter(prefix="/api/assets", tags=["资产管理"])

# 探测结果里可以回写的字段（白名单，避免把任意字段写库）
PROBE_FIELDS = (
    "hostname", "os_version", "kernel", "cpu_cores", "memory_mb",
    "disk_total_gb", "disk_used_percent", "uptime", "status", "last_probe_error",
)


def _log_change(db: Session, asset_id: int, field: str, old, new, operator: str) -> None:
    if str(old or "") == str(new or ""):
        return
    db.add(AssetChangeLog(asset_id=asset_id, field=field,
                          old_value=str(old or ""), new_value=str(new or ""), operator=operator))


def _apply_probe(db: Session, asset: Asset, data: dict, operator: str) -> None:
    for field in PROBE_FIELDS:
        if field in data:
            _log_change(db, asset.id, field, getattr(asset, field), data[field], operator)
            setattr(asset, field, data[field])
    asset.last_probe_at = datetime.now()


# ---------------------------------------------------------------- 分组
@router.get("/groups", summary="分组列表")
def list_groups(_: CurrentUser, db: Session = Depends(get_db)) -> dict:
    rows = db.query(AssetGroup).order_by(AssetGroup.id).all()
    counts = {g.id: db.query(Asset).filter(Asset.group_id == g.id).count() for g in rows}
    return ok([{"id": g.id, "name": g.name, "description": g.description,
                "asset_count": counts.get(g.id, 0)} for g in rows])


@router.post("/groups", summary="新建分组")
def create_group(payload: AssetGroupCreate, _: RequireOps, db: Session = Depends(get_db)) -> dict:
    if db.query(AssetGroup).filter(AssetGroup.name == payload.name).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "分组名已存在")
    group = AssetGroup(name=payload.name, description=payload.description)
    db.add(group)
    db.commit()
    return ok({"id": group.id, "name": group.name})


# ---------------------------------------------------------------- 资产 CRUD
@router.get("", summary="资产列表（支持搜索/分组/环境过滤）")
def list_assets(
    _: CurrentUser,
    db: Session = Depends(get_db),
    keyword: str = "",
    group_id: int | None = None,
    env: str = "",
    status_filter: str = Query("", alias="status"),
    page: int = 1,
    page_size: int = 20,
) -> dict:
    query = db.query(Asset)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(or_(Asset.ip.like(like), Asset.hostname.like(like),
                                 Asset.purpose.like(like), Asset.tags.like(like)))
    if group_id:
        query = query.filter(Asset.group_id == group_id)
    if env:
        query = query.filter(Asset.env == env)
    if status_filter:
        query = query.filter(Asset.status == status_filter)

    total = query.count()
    rows = query.order_by(Asset.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok({
        "items": [AssetOut.model_validate(a).model_dump() for a in rows],
        "total": total, "page": page, "page_size": page_size,
    })


@router.post("", summary="录入资产")
def create_asset(payload: AssetCreate, user: RequireOps, db: Session = Depends(get_db)) -> dict:
    if db.query(Asset).filter(Asset.ip == payload.ip).first():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"资产 {payload.ip} 已存在")
    asset = Asset(**payload.model_dump())
    db.add(asset)
    db.commit()
    db.add(AssetChangeLog(asset_id=asset.id, field="__create__",
                          new_value=f"录入资产 {payload.ip}", operator=user.username))
    db.commit()
    return ok(AssetOut.model_validate(asset).model_dump())


@router.get("/{asset_id}", summary="资产详情")
def get_asset(asset_id: int, _: CurrentUser, db: Session = Depends(get_db)) -> dict:
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在")
    return ok(AssetOut.model_validate(asset).model_dump())


@router.patch("/{asset_id}", summary="修改资产（留痕）")
def update_asset(asset_id: int, payload: AssetUpdate, user: RequireOps,
                 db: Session = Depends(get_db)) -> dict:
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在")
    for field, value in payload.model_dump(exclude_unset=True).items():
        _log_change(db, asset.id, field, getattr(asset, field), value, user.username)
        setattr(asset, field, value)
    db.commit()
    return ok(AssetOut.model_validate(asset).model_dump())


@router.delete("/{asset_id}", summary="删除资产（管理员）")
def delete_asset(asset_id: int, user: RequireAdmin, db: Session = Depends(get_db)) -> dict:
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在")
    db.add(AssetChangeLog(asset_id=asset.id, field="__delete__",
                          old_value=f"{asset.ip}", operator=user.username))
    db.delete(asset)
    db.commit()
    return ok({"deleted": asset_id})


@router.get("/{asset_id}/changes", summary="资产变更记录（审计）")
def asset_changes(asset_id: int, _: CurrentUser, db: Session = Depends(get_db)) -> dict:
    rows = (db.query(AssetChangeLog).filter(AssetChangeLog.asset_id == asset_id)
            .order_by(AssetChangeLog.id.desc()).limit(100).all())
    return ok([
        {"id": r.id, "field": r.field, "old_value": r.old_value, "new_value": r.new_value,
         "operator": r.operator,
         "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""}
        for r in rows
    ])


# ---------------------------------------------------------------- SSH 探测
@router.post("/{asset_id}/probe", summary="SSH 探测单台资产")
def probe_asset(asset_id: int, user: RequireOps, db: Session = Depends(get_db)) -> dict:
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在")
    started = time.perf_counter()
    data = ssh_client.probe(to_target(asset))
    _apply_probe(db, asset, data, user.username)
    db.commit()
    result = ProbeResult(
        asset_id=asset.id,
        ok=data.get("status") == "online",
        data={k: v for k, v in data.items() if k != "last_probe_error"},
        error=data.get("last_probe_error", ""),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
    return ok(result.model_dump())


@router.post("/probe/batch", summary="批量探测（并发）")
def probe_batch(user: RequireOps, asset_ids: list[int] | None = None,
                db: Session = Depends(get_db)) -> dict:
    query = db.query(Asset)
    if asset_ids:
        query = query.filter(Asset.id.in_(asset_ids))
    assets = query.all()
    if not assets:
        return ok([])

    def worker(target: Target) -> dict:
        return ssh_client.probe(target)

    results = []
    for target, data in ssh_client.run_parallel([to_target(a) for a in assets], worker):
        asset = db.get(Asset, target.asset_id)
        if asset:
            _apply_probe(db, asset, data, user.username)
            results.append({"asset_id": asset.id, "ip": asset.ip,
                            "status": data.get("status"),
                            "error": data.get("last_probe_error", "")})
    db.commit()
    online = sum(1 for r in results if r["status"] == "online")
    return ok({"total": len(results), "online": online, "offline": len(results) - online, "items": results})
