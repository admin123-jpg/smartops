"""CI/CD 集成（M6）：GitLab 与 Jenkins 只读对接 + 触发构建。

设计原则：**没配置凭据时整块能力优雅关闭**，接口返回「未配置」而不是报 500，
这样本地演示环境不接 CI 也能跑通全流程。
"""
from __future__ import annotations

import logging
import os
from typing import Any

import requests

log = logging.getLogger(__name__)


def _env(key: str) -> str:
    return os.getenv(key, "").strip()


class GitLabClient:
    def __init__(self) -> None:
        self.base = _env("GITLAB_BASE_URL").rstrip("/")
        self.token = _env("GITLAB_TOKEN")
        self.default_project = _env("GITLAB_PROJECT")

    @property
    def configured(self) -> bool:
        return bool(self.base and self.token)

    def _get(self, path: str, **params: Any) -> Any:
        resp = requests.get(
            f"{self.base}/api/v4{path}",
            headers={"PRIVATE-TOKEN": self.token},
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        resp = requests.post(
            f"{self.base}/api/v4{path}",
            headers={"PRIVATE-TOKEN": self.token},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def pipelines(self, project: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        project = project or self.default_project
        data = self._get(f"/projects/{requests.utils.quote(str(project), safe='')}/pipelines",
                         per_page=limit)
        return [
            {
                "id": p.get("id"),
                "status": p.get("status"),
                "ref": p.get("ref"),
                "sha": (p.get("sha") or "")[:8],
                "web_url": p.get("web_url"),
                "created_at": p.get("created_at"),
            }
            for p in data
        ]

    def trigger(self, ref: str = "main", project: str | None = None,
                variables: dict[str, str] | None = None) -> dict[str, Any]:
        project = project or self.default_project
        data = self._post(f"/projects/{requests.utils.quote(str(project), safe='')}/pipeline",
                          {"ref": ref, "variables": variables or {}})
        return {"id": data.get("id"), "status": data.get("status"),
                "ref": data.get("ref"), "web_url": data.get("web_url")}

    def jobs(self, pipeline_id: int, project: str | None = None) -> list[dict[str, Any]]:
        project = project or self.default_project
        data = self._get(f"/projects/{requests.utils.quote(str(project), safe='')}"
                         f"/pipelines/{pipeline_id}/jobs")
        return [{"name": j.get("name"), "stage": j.get("stage"), "status": j.get("status"),
                 "duration": j.get("duration")} for j in data]


class JenkinsClient:
    def __init__(self) -> None:
        self.base = _env("JENKINS_BASE_URL").rstrip("/")
        self.user = _env("JENKINS_USER")
        self.token = _env("JENKINS_TOKEN")

    @property
    def configured(self) -> bool:
        return bool(self.base)

    def _auth(self) -> tuple[str, str] | None:
        return (self.user, self.token) if self.user and self.token else None

    def jobs(self) -> list[dict[str, Any]]:
        resp = requests.get(f"{self.base}/api/json",
                            params={"tree": "jobs[name,color,url,lastBuild[number,result,timestamp]]"},
                            auth=self._auth(), timeout=10)
        resp.raise_for_status()
        out = []
        for job in resp.json().get("jobs", []):
            last = job.get("lastBuild") or {}
            out.append({
                "name": job.get("name"),
                "color": job.get("color"),
                "url": job.get("url"),
                "last_build": last.get("number"),
                "result": last.get("result"),
                "timestamp": last.get("timestamp"),
            })
        return out

    def job_status(self, name: str) -> dict[str, Any]:
        resp = requests.get(f"{self.base}/job/{requests.utils.quote(name, safe='')}/api/json",
                            auth=self._auth(), timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return {"name": data.get("name"), "color": data.get("color"),
                "last_build": (data.get("lastBuild") or {}).get("number"),
                "result": (data.get("lastBuild") or {}).get("result"),
                "health": (data.get("healthReport") or [{}])[0].get("score")}

    def build(self, name: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        path = f"/job/{requests.utils.quote(name, safe='')}/build"
        resp = requests.post(
            f"{self.base}{path}", params=params or {}, auth=self._auth(), timeout=10
        )
        return {"queued": resp.status_code in (200, 201, "200", "201"),
                "location": resp.headers.get("Location", "")}


gitlab = GitLabClient()
jenkins = JenkinsClient()


def status() -> dict[str, Any]:
    return {
        "gitlab": {"configured": gitlab.configured, "base": gitlab.base or "", "project": gitlab.default_project},
        "jenkins": {"configured": jenkins.configured, "base": jenkins.base or ""},
    }
