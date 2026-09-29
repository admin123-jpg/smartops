"""日志分析工具接口（M5）。

三种取日志的方式：
  1. 直接上传文件（浏览器选文件）
  2. 指定远程路径 —— 平台用 SFTP 从被管服务器拉回来（被管机零侵入，不用装任何东西）
  3. 直接贴一段文本（调试用）

分析在本地做，不在远端做：远端不一定有完整的 awk/sed，也不该往业务机上装解析器。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.asset import Asset
from app.services import llm_client, log_parser, ssh_client
from app.tasks.batch_exec import to_target
from app.utils.response import ok
from app.utils.security import CurrentUser, RequireOps

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/logs", tags=["日志分析"])

MAX_UPLOAD = 20 * 1024 * 1024


class RemoteLogRequest(BaseModel):
    asset_id: int
    remote_path: str = Field(..., description="服务器上的日志绝对路径")
    slow_threshold: float = Field(1.0, description="慢请求阈值（秒），仅对 Nginx 访问日志生效")
    diagnose: bool = Field(False, description="是否额外调用大模型做根因分析")


def _to_text(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@router.post("/analyze", summary="上传日志文件并分析")
async def analyze_upload(_: RequireOps,
                         file: UploadFile = File(...),
                         slow_threshold: float = Form(1.0),
                         diagnose: bool = Form(False)) -> dict:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文件超过 20MB，请先切分")
    text = _to_text(raw)
    result = log_parser.analyze(text, slow_threshold, source=file.filename or "upload")

    payload = {"analysis": result}
    if diagnose:
        ctx = log_parser.build_llm_context(text, result)
        payload["ai"] = llm_client.analyze_log(ctx)
    return ok(payload)


@router.post("/remote", summary="从被管服务器拉取日志并分析")
def analyze_remote(payload: RemoteLogRequest, _: RequireOps,
                   db: Session = Depends(get_db)) -> dict:
    asset = db.get(Asset, payload.asset_id)
    if not asset:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "资产不存在")

    settings.ensure_dirs()
    local = settings.DATA_DIR / "tmp" / f"pull_{asset.id}_{int(time.time())}.log"
    local.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    result = ssh_client.download(to_target(asset), payload.remote_path, str(local))
    if not result.ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            f"拉取失败：{result.error or result.stderr}")

    text = _to_text(local.read_bytes())
    analysis = log_parser.analyze(text, payload.slow_threshold,
                                  source=f"{asset.hostname or asset.ip}:{payload.remote_path}")
    analysis["pull_ms"] = int((time.perf_counter() - started) * 1000)
    analysis["size_kb"] = round(local.stat().st_size / 1024, 1)

    payload_out = {"analysis": analysis}
    if payload.diagnose:
        ctx = log_parser.build_llm_context(text, analysis)
        payload_out["ai"] = llm_client.analyze_log(ctx)
    return ok(payload_out)


class InlineLogRequest(BaseModel):
    text: str = Field(..., description="直接贴日志内容")
    slow_threshold: float = 1.0
    diagnose: bool = False
    source: str = "inline"


@router.post("/inline", summary="直接分析一段日志文本")
def analyze_inline(payload: InlineLogRequest, _: RequireOps) -> dict:
    if not payload.text.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "日志内容为空")
    analysis = log_parser.analyze(payload.text, payload.slow_threshold, source=payload.source)
    out = {"analysis": analysis}
    if payload.diagnose:
        out["ai"] = llm_client.analyze_log(log_parser.build_llm_context(payload.text, analysis))
    return ok(out)


@router.get("/demo-samples", summary="示例日志（一键体验分析效果）")
def demo_samples(_: CurrentUser) -> dict:
    """给一份带真实故障特征的 Nginx 访问日志样本，方便直接看分析效果。"""
    lines = []
    codes = ["200"] * 8 + ["502", "504", "200", "500"]
    for i in range(120):
        ip = f"10.244.{i % 4}.{20 + i % 9}"
        code = codes[i % len(codes)]
        rt = "0.03" if code == "200" else f"{1.5 + (i % 7) * 0.4:.2f}"
        url = "/api/diagnose" if i % 3 else "/static/app.js"
        lines.append(
            f'{ip} - - [29/Sep/2026:10:{i % 60:02d}:11 +0800] "GET {url} HTTP/1.1" '
            f'{code} {1200 + i} "-" "Mozilla/5.0" {rt}'
        )
    return ok({"source": "nginx-access-demo.log", "text": "\n".join(lines)})
