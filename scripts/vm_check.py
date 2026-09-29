# -*- coding: utf-8 -*-
"""验证三台虚拟机的 SSH 连通性并采集基础信息（输出写入文件，避免日志刷屏）。"""
import logging
from concurrent.futures import ThreadPoolExecutor

logging.getLogger("paramiko").setLevel(logging.CRITICAL)
import paramiko  # noqa: E402

from _env import SSH_PASSWORD, SSH_PORT, SSH_USER, require_password, require_targets  # noqa: E402

IPS = require_targets()
PASSWORD = require_password()

CMD = (
    'echo "HOST=$(hostname)"; '
    'echo "OS=$(cat /etc/redhat-release 2>/dev/null || cat /etc/os-release 2>/dev/null | head -1)"; '
    'echo "KERNEL=$(uname -r)"; '
    'echo "CPU=$(nproc)"; '
    'echo "MEM=$(free -m | awk \'/Mem:/{print $2}\')MB"; '
    'echo "DISK=$(df -P / | awk \'NR==2{print $2" "$5}\')"; '
    'echo "UPTIME=$(uptime -p 2>/dev/null || uptime)"; '
    'echo "KUBELET=$(systemctl is-active kubelet 2>/dev/null || echo none)"; '
    'echo "CONTAINERD=$(systemctl is-active containerd 2>/dev/null || echo none)"; '
    'echo "DOCKER=$(systemctl is-active docker 2>/dev/null || echo none)"; '
    'echo "KUBEVER=$(kubelet --version 2>/dev/null | awk \'{print $2}\')"; '
    'echo "IP=$(hostname -I 2>/dev/null)"; '
    'echo "--- kube nodes ---"; kubectl get nodes -o wide --no-headers 2>/dev/null | head -5'
)


def check(ip):
    lines = [f"========== {ip} =========="]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(ip, port=SSH_PORT, username=SSH_USER, password=PASSWORD, timeout=8,
                       banner_timeout=10, auth_timeout=10, allow_agent=False, look_for_keys=False)
        _in, out, err = client.exec_command(CMD, timeout=20)
        lines.append(out.read().decode("utf-8", errors="replace").strip())
        e = err.read().decode("utf-8", errors="replace").strip()
        if e:
            lines.append("[stderr] " + e[:300])
        lines.append(">>> SSH 登录成功")
    except Exception as exc:
        lines.append(f">>> 失败：{type(exc).__name__}: {exc}")
    finally:
        client.close()
    return "\n".join(lines)


with ThreadPoolExecutor(max_workers=3) as pool:
    results = list(pool.map(check, IPS))

open(r"E:\smartops\scripts\vm_check.txt", "w", encoding="utf-8").write("\n\n".join(results))
print("done")
