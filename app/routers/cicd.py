"""CI/CD 集成接口（M6）。

未配置凭据时所有接口返回 configured=false，而不是报错 —— 本地演示环境不接
GitLab/Jenkins 也能把整条链路跑通。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.services import cicd_client
from app.utils.response import ok
from app.utils.security import CurrentUser, RequireOps

router = APIRouter(prefix="/api/cicd", tags=["CI/CD 集成"])


class TriggerRequest(BaseModel):
    ref: str = "main"
    project: str = ""
    variables: dict[str, str] = {}


class JenkinsBuildRequest(BaseModel):
    job: str
    params: dict[str, str] = {}


@router.get("/status", summary="CI/CD 对接状态")
def cicd_status(_: CurrentUser) -> dict:
    return ok(cicd_client.status())


# ---------------------------------------------------------------- GitLab
@router.get("/gitlab/pipelines", summary="最近的流水线记录")
def gitlab_pipelines(_: CurrentUser, limit: int = 20, project: str = "") -> dict:
    if not cicd_client.gitlab.configured:
        return ok({"configured": False, "hint": "请在 .env 配置 GITLAB_BASE_URL / GITLAB_TOKEN / GITLAB_PROJECT",
                   "items": []})
    try:
        return ok({"configured": True, "items": cicd_client.gitlab.pipelines(project or None, limit)})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"GitLab 调用失败：{exc}")


@router.post("/gitlab/trigger", summary="触发一次流水线（一键发布）")
def gitlab_trigger(payload: TriggerRequest, _: RequireOps) -> dict:
    if not cicd_client.gitlab.configured:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "GitLab 未配置")
    try:
        return ok(cicd_client.gitlab.trigger(payload.ref, payload.project or None, payload.variables))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"触发失败：{exc}")


@router.get("/gitlab/pipelines/{pipeline_id}/jobs", summary="流水线内的作业详情")
def gitlab_jobs(pipeline_id: int, _: CurrentUser, project: str = "") -> dict:
    if not cicd_client.gitlab.configured:
        return ok({"configured": False, "items": []})
    try:
        return ok({"configured": True, "items": cicd_client.gitlab.jobs(pipeline_id, project or None)})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"查询失败：{exc}")


# ---------------------------------------------------------------- Jenkins
@router.get("/jenkins/jobs", summary="Jenkins 作业列表与最近构建结果")
def jenkins_jobs(_: CurrentUser) -> dict:
    if not cicd_client.jenkins.configured:
        return ok({"configured": False, "hint": "请在 .env 配置 JENKINS_BASE_URL（可按需配 JENKINS_USER/JENKINS_TOKEN）",
                   "items": []})
    try:
        return ok({"configured": True, "items": cicd_client.jenkins.jobs()})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Jenkins 调用失败：{exc}")


@router.get("/jenkins/jobs/{name}", summary="单个作业状态")
def jenkins_job(name: str, _: CurrentUser) -> dict:
    if not cicd_client.jenkins.configured:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Jenkins 未配置")
    try:
        return ok(cicd_client.jenkins.job_status(name))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"查询失败：{exc}")


@router.post("/jenkins/build", summary="触发构建 / 回滚")
def jenkins_build(payload: JenkinsBuildRequest, _: RequireOps) -> dict:
    if not cicd_client.jenkins.configured:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Jenkins 未配置")
    try:
        return ok(cicd_client.jenkins.build(payload.job, payload.params))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"触发失败：{exc}")
