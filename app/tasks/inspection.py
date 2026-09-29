"""自动化巡检任务（M3）。

除了逐项判级，这里还做两件"让巡检报告能直接发给主管"的事：
  1. 用 Jinja2 渲染一份自带样式的 HTML 报告（异常项红色高亮、可一键跳转修复）；
  2. 把异常项清单交给大模型，生成一段自然语言的「整体结论 + 优先处理项」，
     省掉人工写总结 —— 这是 AI 在运维里最不起眼但最省时间的用法。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings
from app.db import SessionLocal
from app.models.asset import Asset
from app.models.inspection import (
    LEVEL_CRITICAL,
    LEVEL_ERROR,
    LEVEL_OK,
    LEVEL_WARN,
    InspectionResult,
    InspectionRun,
    InspectionTemplate,
)
from app.services import inspector, llm_client
from app.services.ssh_client import Target
from app.tasks.batch_exec import to_target, _publish_progress
from app.tasks.celery_app import celery_app

log = logging.getLogger(__name__)

_env = Environment(
    loader=FileSystemLoader(str(settings.BASE_DIR / "app" / "templates")),
    autoescape=select_autoescape(["html"]),
)


def run_inspection(db, run: InspectionRun, targets: list[Asset], checks: list[dict]) -> dict:
    """对一批目标执行巡检并落库。返回统计。"""
    asset_map = {a.id: a for a in targets}
    all_results: list[dict] = []

    def worker(target: Target) -> dict:
        asset = asset_map.get(target.asset_id)
        row = inspector.inspect_one(target, checks, timeout=settings.SSH_TIMEOUT + 20)
        row["asset_label"] = f"{asset.hostname or ''}({asset.ip})" if asset else target.ip
        return row

    from app.services.ssh_client import run_parallel

    for target, row in run_parallel([to_target(a) for a in targets], worker):
        for item in row.get("results", []):
            db.add(InspectionResult(
                run_id=run.id,
                asset_id=target.asset_id,
                asset_label=row.get("asset_label", target.ip),
                item_type=item["item_type"],
                item_name=item["item_name"],
                level=item["level"],
                value=item["value"],
                message=item["message"],
                suggestion="",
            ))
            all_results.append({**item, "asset_label": row.get("asset_label", target.ip)})

    stat = inspector.summarize(all_results)
    run.total_items = len(all_results)
    run.ok_items = stat.get(LEVEL_OK, 0)
    run.warn_items = stat.get(LEVEL_WARN, 0)
    run.critical_items = stat.get(LEVEL_CRITICAL, 0) + stat.get(LEVEL_ERROR, 0)
    run.status = "finished"
    run.finished_at = datetime.now()
    db.commit()

    # ---------------- AI 总结 ----------------
    abnormal = [r for r in all_results if r["level"] != LEVEL_OK]
    if abnormal:
        abnormal_text = "\n".join(
            f"- [{r['level']}] {r['asset_label']} | {r['item_name']}: {r['message']}"
            for r in abnormal[:40]
        )
        ai = llm_client.summarize_inspection({
            "template_name": run.template_name,
            "asset_count": len(targets),
            "ok_items": run.ok_items,
            "warn_items": run.warn_items,
            "critical_items": run.critical_items,
            "abnormal_text": abnormal_text,
        })
        run.ai_summary = (
            f"【结论】{ai.get('root_cause', '')}\n"
            f"【依据】{'；'.join(ai.get('evidence', []) or [])}\n"
            f"【优先处理】\n{llm_client.render_suggestions(ai)}\n"
            f"（来源：{'大模型 ' + str(ai.get('model', '')) if ai.get('mode') == 'llm' else '内置规则库'}）"
        )
        db.commit()

    # ---------------- 渲染报告 ----------------
    report_rows = (
        db.query(InspectionResult)
        .filter(InspectionResult.run_id == run.id)
        .order_by(InspectionResult.level.desc(), InspectionResult.asset_label)
        .all()
    )
    html = _env.get_template("report.html").render(
        run=run,
        rows=report_rows,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        level_ok=LEVEL_OK, level_warn=LEVEL_WARN, level_critical=LEVEL_CRITICAL,
    )
    report_path = settings.REPORT_DIR / f"inspection_{run.id}.html"
    report_path.write_text(html, encoding="utf-8")
    run.report_path = str(report_path)
    db.commit()

    return {
        "run_id": run.id,
        "total": len(all_results),
        "ok": run.ok_items,
        "warn": run.warn_items,
        "critical": run.critical_items,
        "report_path": str(report_path),
    }


@celery_app.task(name="app.tasks.inspection.run_inspection", bind=True)
def run_inspection_task(self, run_id: int) -> dict:
    db = SessionLocal()
    try:
        run = db.get(InspectionRun, run_id)
        if not run:
            return {"ok": False, "error": f"巡检批次 {run_id} 不存在"}
        template = db.get(InspectionTemplate, run.template_id)
        checks = inspector.parse_checks(template.checks_json if template else "[]") or inspector.BUILTIN_CHECKS

        ids = [int(x) for x in (run.asset_ids or "").split(",") if x.strip().isdigit()]
        assets = db.query(Asset).filter(Asset.id.in_(ids)).all() if ids else []
        if not assets:
            run.status = "failed"
            run.finished_at = datetime.now()
            db.commit()
            return {"ok": False, "error": "没有找到目标资产"}

        run.status = "running"
        db.commit()
        _publish_progress(run.id, {"run_id": run.id, "status": "running", "total": len(assets)})

        result = run_inspection(db, run, assets, checks)
        _publish_progress(run.id, {"run_id": run.id, "status": "finished", **result})
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        log.exception("巡检批次 %s 执行异常", run_id)
        try:
            run = db.get(InspectionRun, run_id)
            if run:
                run.status = "failed"
                run.finished_at = datetime.now()
                db.commit()
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.inspection.scheduled_inspection")
def scheduled_inspection(template_id: int | None = None) -> dict:
    """定时巡检（celery beat 每 6 小时触发一次）。"""
    db = SessionLocal()
    try:
        template = db.get(InspectionTemplate, template_id) if template_id else \
            db.query(InspectionTemplate).filter(InspectionTemplate.is_builtin == 1).first()
        if not template:
            return {"ok": False, "error": "未找到巡检模板"}
        assets = db.query(Asset).filter(Asset.status == "online").all()
        if not assets:
            return {"ok": False, "error": "没有在线资产"}
        run = InspectionRun(
            template_id=template.id,
            template_name=template.name,
            asset_ids=",".join(str(a.id) for a in assets),
            status="pending",
            trigger="cron",
            operator="system",
        )
        db.add(run)
        db.commit()
        return run_inspection_task.delay(run.id) and {"ok": True, "run_id": run.id}
    finally:
        db.close()
