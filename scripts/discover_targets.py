# -*- coding: utf-8 -*-
"""发现本机所在网段、扫描 22 端口，并尝试用环境变量里的凭据登录，确认可用的被管目标。

被管机凭据从 `_env` 读（SMARTOPS_SSH_USER / SMARTOPS_SSH_PASSWORD），不写死在代码里。
"""
import socket
import concurrent.futures as cf
import logging

# paramiko 在探测失败时会往 root logger 打大量 traceback，压掉
logging.getLogger("paramiko").setLevel(logging.CRITICAL)
logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)

from _env import SSH_PASSWORD, SSH_PORT, SSH_USER  # noqa: E402

LOCAL = []
for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
    LOCAL.append(info[4][0])


def subnets():
    nets = set()
    for ip in LOCAL:
        if ip.startswith("127."):
            continue
        nets.add(ip.rsplit(".", 1)[0])
    return sorted(nets)


def probe(ip):
    s = socket.socket()
    s.settimeout(0.35)
    try:
        s.connect((ip, 22))
        return ip
    except Exception:
        return None
    finally:
        s.close()


found = []
targets = []
for net in subnets():
    targets += [f"{net}.{i}" for i in range(1, 255)]

with cf.ThreadPoolExecutor(max_workers=200) as pool:
    for result in pool.map(probe, targets):
        if result:
            found.append(result)

out_lines = []
def log(*a):
    out_lines.append(" ".join(str(x) for x in a))


log("本机 IP:", LOCAL)
log("扫描网段:", subnets())
log("开放 22 端口的主机:", found or "（无）")

try:
    import paramiko
except ImportError:
    paramiko = None

if paramiko:
    log("")
    log("--- 尝试 root/1 登录 ---")
    for ip in found:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(ip, port=SSH_PORT, username=SSH_USER, password=SSH_PASSWORD, timeout=6,
                      allow_agent=False, look_for_keys=False, banner_timeout=8, auth_timeout=8)
            cmd = ("hostname; cat /etc/redhat-release 2>/dev/null; echo CPU=$(nproc); "
                   "free -m | awk '/Mem:/{print \"MEM=\"$2\"MB\"}'; "
                   "echo KUBELET=$(systemctl is-active kubelet 2>/dev/null); "
                   "echo CONTAINERD=$(systemctl is-active containerd 2>/dev/null); "
                   "echo DOCKER=$(systemctl is-active docker 2>/dev/null); "
                   "kubectl get nodes --no-headers 2>/dev/null | head -5")
            _, out, _ = c.exec_command(cmd, timeout=15)
            text = out.read().decode("utf-8", errors="replace").strip()
            log(f"[OK] {ip} ->")
            log(text)
            log("")
        except Exception as exc:
            log(f"[FAIL] {ip}: {type(exc).__name__} {exc}")
        finally:
            c.close()

open(r"E:\smartops\scripts\discover_result.txt", "w", encoding="utf-8").write("\n".join(out_lines))
print("done ->", len(found), "hosts on port 22")
