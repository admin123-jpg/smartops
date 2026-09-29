"""配置中心：统一读取 .env，并为本地开发提供安全默认值。

设计取舍：
- 本地开发默认 SQLite（零依赖、即开即用），生产用 MySQL —— 通过 DATABASE_URL 切换。
- 大模型配置与 ai-log-doctor 保持一致：兼容 DeepSeek / 通义 / OpenAI 的 OpenAI 风格接口。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    # ---------- 路径 ----------
    BASE_DIR: Path = BASE_DIR

    # ---------- 基础 ----------
    APP_NAME: str = "SmartOps Assistant"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = _bool("DEBUG", True)
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = int(os.getenv("PORT", "8100"))

    # ---------- 数据层 ----------
    # 本地默认 SQLite；生产用 mysql+pymysql://user:pwd@host:3306/smartops?charset=utf8mb4
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", f"sqlite:///{(BASE_DIR / 'data' / 'smartops.db').as_posix()}"
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

    # ---------- 认证 ----------
    JWT_SECRET: str = os.getenv("JWT_SECRET", "smartops-dev-secret-change-me")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = int(os.getenv("JWT_EXPIRE_MINUTES", "720"))

    # ---------- SSH 批量操作 ----------
    SSH_TIMEOUT: int = int(os.getenv("SSH_TIMEOUT", "10"))
    SSH_MAX_CONCURRENCY: int = int(os.getenv("SSH_MAX_CONCURRENCY", "10"))
    # 命中这些关键词的命令需要二次确认
    DANGEROUS_KEYWORDS: tuple[str, ...] = (
        "rm -rf", "mkfs", "shutdown", "reboot", "init 0", "init 6",
        "dd if=", ":(){", "> /dev/sda", "chmod -R 777 /", "iptables -F",
    )

    # ---------- 巡检 ----------
    INSPECT_DISK_WARN: int = int(os.getenv("INSPECT_DISK_WARN", "80"))
    INSPECT_DISK_CRIT: int = int(os.getenv("INSPECT_DISK_CRIT", "90"))
    INSPECT_MEM_WARN: int = int(os.getenv("INSPECT_MEM_WARN", "85"))
    INSPECT_LOAD_WARN: float = float(os.getenv("INSPECT_LOAD_WARN", "4"))

    # ---------- 大模型（AI 智能诊断）----------
    LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "deepseek")
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-chat")
    LLM_TIMEOUT: int = int(os.getenv("LLM_TIMEOUT", "60"))
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "1200"))

    # ---------- 告警 ----------
    ALERT_DEDUP_WINDOW: int = int(os.getenv("ALERT_DEDUP_WINDOW", "300"))  # 秒
    ALERT_WEBHOOK_TOKEN: str = os.getenv("ALERT_WEBHOOK_TOKEN", "")

    # ---------- 报告 ----------
    REPORT_DIR: Path = BASE_DIR / "data" / "reports"
    DATA_DIR: Path = BASE_DIR / "data"

    def ensure_dirs(self) -> None:
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.REPORT_DIR.mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
