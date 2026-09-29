"""SmartOps Assistant —— FastAPI 应用入口。

启动：
    E:\\smartops\\venv\\Scripts\\python.exe -m app.main              # 生产式启动
    E:\\smartops\\venv\\Scripts\\python.exe -m app.main --reload     # 开发热重载
"""
from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import SessionLocal, init_db
from app.routers import alerts, assets, auth, cicd, dashboard, executor, inspection, logs
from app.routers.inspection import ensure_builtin_template
from app.utils.security import ROLE_ADMIN, hash_password
from app.models.user import User

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("smartops")


def seed_default_users() -> None:
    """首次启动创建三个演示账号，分别对应三种角色。"""
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            return
        defaults = [
            ("admin", "admin123", "系统管理员", ROLE_ADMIN),
            ("ops", "ops123", "运维工程师", "ops"),
            ("viewer", "view123", "只读用户", "readonly"),
        ]
        for username, pwd, display, role in defaults:
            db.add(User(username=username, password_hash=hash_password(pwd),
                        display_name=display, role=role))
        db.commit()
        log.info("已创建默认账号：admin/admin123、ops/ops123、viewer/view123")
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.ensure_dirs()
    init_db()
    seed_default_users()
    db = SessionLocal()
    try:
        ensure_builtin_template(db)
    finally:
        db.close()
    log.info("SmartOps 已启动 | 数据库=%s | Redis=%s",
             settings.DATABASE_URL.split("://")[0], settings.REDIS_URL)
    yield
    log.info("SmartOps 已停止")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="轻量级智能运维平台：资产管理 · 批量执行 · 自动化巡检 · 告警聚合 · 日志分析 · CI/CD · AI 诊断",
    lifespan=lifespan,
)

# 本地演示放开跨域；生产应收紧为具体域名
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理：返回结构化错误，不把堆栈直接抛给前端。"""
    log.exception("未处理异常 %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"code": 500, "message": f"服务器内部错误：{exc}", "data": None})


# ---------------- 业务路由 ----------------
app.include_router(auth.router)
app.include_router(assets.router)
app.include_router(executor.router)
app.include_router(inspection.router)
app.include_router(alerts.router)
app.include_router(logs.router)
app.include_router(cicd.router)
app.include_router(dashboard.router)
app.include_router(dashboard.ai_router)


@app.get("/api/health", tags=["运维"], summary="健康检查")
def health() -> dict:
    from app.tasks.celery_app import broker_status

    return {"code": 0, "message": "ok",
            "data": {"app": settings.APP_NAME, "version": settings.APP_VERSION, **broker_status()}}


# ---------------- 前端页面 ----------------
app.mount("/static", StaticFiles(directory=str(settings.BASE_DIR / "frontend")), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(str(settings.BASE_DIR / "frontend" / "index.html"))


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 SmartOps Assistant")
    parser.add_argument("--host", default=settings.HOST)
    parser.add_argument("--port", type=int, default=settings.PORT)
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run("app.main:app" if args.reload else app, host=args.host, port=args.port,
                reload=args.reload, log_level="info")


if __name__ == "__main__":
    main()
