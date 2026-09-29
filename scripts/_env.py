# -*- coding: utf-8 -*-
"""测试脚本共用的环境配置读取。

【为什么单独抽这个文件】
被管机的地址和 SSH 凭据**不要写死在代码里** —— 一旦提交进公开仓库就收不回来了。
所以统一从环境变量读，代码里只留不敏感的默认值。

用法（PowerShell）：
    $env:SMARTOPS_TARGET_IPS   = "192.168.1.10,192.168.1.11,192.168.1.12"
    $env:SMARTOPS_SSH_USER     = "root"
    $env:SMARTOPS_SSH_PASSWORD = "你的密码"
    python scripts/demo_3nodes.py

用法（bash）：
    SMARTOPS_TARGET_IPS=192.168.1.10,192.168.1.11 \
    SMARTOPS_SSH_PASSWORD=xxxx \
    python scripts/demo_3nodes.py
"""
from __future__ import annotations

import os

# ---------------- 平台自身 ----------------
BASE = os.getenv("SMARTOPS_BASE", "http://127.0.0.1:8100")

# 这三个是平台自带的演示账号（不是被管机凭据），可以公开
APP_USER = os.getenv("SMARTOPS_USER", "admin")
APP_PASSWORD = os.getenv("SMARTOPS_PASSWORD", "admin123")
APP_VIEWER = os.getenv("SMARTOPS_VIEWER", "viewer")
APP_VIEWER_PASSWORD = os.getenv("SMARTOPS_VIEWER_PASSWORD", "view123")

# ---------------- 被管目标机（必须自己配）----------------
SSH_USER = os.getenv("SMARTOPS_SSH_USER", "root")
SSH_PASSWORD = os.getenv("SMARTOPS_SSH_PASSWORD", "")
SSH_PORT = int(os.getenv("SMARTOPS_SSH_PORT", "22"))

TARGET_IPS: list[str] = [
    x.strip() for x in os.getenv("SMARTOPS_TARGET_IPS", "").split(",") if x.strip()
]

# 内置巡检项里要检查的进程与端口，按你的环境改
CHECK_PROCESSES = [x for x in os.getenv("SMARTOPS_CHECK_PROCESSES", "sshd,crond").split(",") if x]
CHECK_PORTS = [int(x) for x in os.getenv("SMARTOPS_CHECK_PORTS", "22").split(",") if x.strip()]


def require_targets() -> list[str]:
    """没配置被管机就直接说清楚，别让脚本跑到一半才报错。"""
    if not TARGET_IPS:
        raise SystemExit(
            "未配置被管机（SMARTOPS_TARGET_IPS 为空）。示例：\n"
            '  PowerShell: $env:SMARTOPS_TARGET_IPS="192.168.1.10,192.168.1.11"\n'
            "  bash:       SMARTOPS_TARGET_IPS=192.168.1.10,192.168.1.11 python scripts/demo_3nodes.py\n"
            "也可以直接用网页界面手动录入资产，不跑这些脚本。"
        )
    return TARGET_IPS


def require_password() -> str:
    """SSH 密码也要求显式配置，避免代码里留一个「默认密码」。"""
    if not SSH_PASSWORD:
        raise SystemExit(
            "未配置 SSH 密码（SMARTOPS_SSH_PASSWORD 为空）。\n"
            "如果你用的是密钥登录，请在网页界面选择密钥认证方式。"
        )
    return SSH_PASSWORD
