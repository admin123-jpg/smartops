"""SSH 能力封装（paramiko）。

管家资产探测 / 批量执行 / 巡检 三块都靠它，所以这里把「连接、执行、断链、超时」
这些容易出问题的细节统一处理掉：

- 每次执行用独立 channel，设置 recv 超时，避免对端不返回时永久阻塞；
- stdout / stderr 分开取，退出码通过 exit_status_ready 拿；
- 大批量目标用线程池并发，但限制最大并发数（settings.SSH_MAX_CONCURRENCY），
  避免一次开几百个连接把自己打挂；
- 任何异常都转成结构化结果，不让一个失败目标拖垮整批任务。
"""
from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator

import paramiko

from app.config import settings

log = logging.getLogger(__name__)

# 一次 SSH 会话采集全部资产信息的脚本，用 ### 分节便于解析
# 一次 SSH 会话采集全部资产信息的脚本，用 ### 分节便于解析。
# 注意两点（都是踩过的坑）：
#  1. 分节标记必须独占一行，所以每个 printf 末尾都要带 \n —— 否则下一节的 ###XXX
#     会被粘在数据后面，导致这一节整段解析失败（表现为资产探测回来全是 0）。
#  2. 最后一节必须有输出且以换行为结尾，保证脚本退出码为 0。
PROBE_SCRIPT = r"""
echo "###HOSTNAME"; hostname 2>/dev/null
echo "###OS"; (cat /etc/redhat-release 2>/dev/null || (cat /etc/os-release 2>/dev/null | sed -n '1p'))
echo "###KERNEL"; uname -r 2>/dev/null
echo "###CPU"; nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo
echo "###MEM"; awk '/MemTotal/{printf "%d\n", $2/1024}' /proc/meminfo
echo "###DISK"; df -P / 2>/dev/null | awk 'NR==2{printf "%s %s\n", $2, $5}'
echo "###UPTIME"; (uptime -p 2>/dev/null || uptime)
echo "###END"
"""


@dataclass
class SSHResult:
    ok: bool
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


@dataclass
class Target:
    """一个连接目标（资产表的轻量投影，避免服务层依赖 ORM）。"""

    asset_id: int
    ip: str
    ssh_port: int = 22
    ssh_user: str = "root"
    auth_type: str = "password"
    password: str = ""
    private_key_path: str = ""
    label: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _build_client() -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    # 生产上应改为加载 known_hosts 并严格校验；这里为便于内网批量管理放开
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    return client


def _connect(target: Target, timeout: int | None = None) -> paramiko.SSHClient:
    timeout = timeout or settings.SSH_TIMEOUT
    client = _build_client()
    kwargs: dict[str, Any] = {
        "hostname": target.ip,
        "port": target.ssh_port or 22,
        "username": target.ssh_user or "root",
        "timeout": timeout,
        "banner_timeout": timeout,
        "auth_timeout": timeout,
        # 心跳：网络抖动或长时间无输出时保活，同时能探测断链
        "channel_timeout": timeout,
    }
    if target.auth_type == "key" and target.private_key_path:
        kwargs["key_filename"] = target.private_key_path
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False
    else:
        kwargs["password"] = target.password
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False
    client.connect(**kwargs)
    transport = client.get_transport()
    if transport:
        transport.set_keepalive(30)
    return client


def exec_command(target: Target, command: str, timeout: int | None = None) -> SSHResult:
    """在单台目标上执行命令。永远返回 SSHResult，不抛异常。"""
    started = time.perf_counter()
    timeout = timeout or settings.SSH_TIMEOUT
    client = None
    try:
        client = _connect(target, timeout)
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout, get_pty=False)
        channel = stdout.channel
        channel.settimeout(timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        code = channel.recv_exit_status()
        return SSHResult(
            ok=code == 0,
            exit_code=code,
            stdout=out,
            stderr=err,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    except paramiko.AuthenticationException as exc:
        return SSHResult(False, error=f"认证失败：{exc}", duration_ms=int((time.perf_counter() - started) * 1000))
    except paramiko.SSHException as exc:
        return SSHResult(False, error=f"SSH 协议错误：{exc}", duration_ms=int((time.perf_counter() - started) * 1000))
    except TimeoutError:
        return SSHResult(False, error=f"连接/执行超时（{timeout}s）", duration_ms=int((time.perf_counter() - started) * 1000))
    except OSError as exc:
        return SSHResult(False, error=f"网络不可达：{exc}", duration_ms=int((time.perf_counter() - started) * 1000))
    except Exception as exc:  # noqa: BLE001
        return SSHResult(False, error=f"未知错误：{exc}", duration_ms=int((time.perf_counter() - started) * 1000))
    finally:
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def probe(target: Target) -> dict[str, Any]:
    """采集目标机器的基础信息，返回可直接写回资产表的字典。"""
    result = exec_command(target, PROBE_SCRIPT, timeout=max(settings.SSH_TIMEOUT, 15))
    if not result.ok:
        return {
            "status": "offline",
            "last_probe_error": result.error or f"退出码 {result.exit_code}",
        }

    info: dict[str, Any] = {"status": "online", "last_probe_error": ""}
    for section, body in re.findall(r"###(\w+)\n(.*?)(?=\n###|\Z)", result.stdout, re.S):
        value = body.strip().splitlines()[0].strip() if body.strip() else ""
        key = section.lower()
        if key == "hostname":
            info["hostname"] = value
        elif key == "os":
            info["os_version"] = value[:128]
        elif key == "kernel":
            info["kernel"] = value[:128]
        elif key == "cpu":
            try:
                info["cpu_cores"] = int(re.sub(r"\D", "", value) or 0)
            except ValueError:
                info["cpu_cores"] = 0
        elif key == "mem":
            try:
                info["memory_mb"] = int(re.sub(r"\D", "", value) or 0)
            except ValueError:
                info["memory_mb"] = 0
        elif key == "disk":
            parts = value.split()
            if len(parts) >= 2:
                info["disk_total_gb"] = _to_gb(parts[0])
                info["disk_used_percent"] = float(re.sub(r"[^\d.]", "", parts[1]) or 0)
        elif key == "uptime":
            info["uptime"] = value[:128]
    return info


def _to_gb(text: str) -> float:
    """把 df 的 40G / 500M / 1.8T 统一换算成 GB。"""
    m = re.match(r"([\d.]+)\s*([KMGT]?)", text.strip(), re.I)
    if not m:
        return 0.0
    size = float(m.group(1))
    unit = (m.group(2) or "").upper()
    factor = {"": 1 / 1024 / 1024, "K": 1 / 1024 / 1024, "M": 1 / 1024, "G": 1, "T": 1024}.get(unit, 1)
    return round(size * factor, 2)


def upload(target: Target, local_path: str, remote_path: str) -> SSHResult:
    """推送文件（SFTP）。先传临时文件再 mv，避免传到一半被读到。"""
    started = time.perf_counter()
    client = None
    try:
        client = _connect(target)
        sftp = client.open_sftp()
        tmp = f"{remote_path}.smartops.tmp"
        sftp.put(local_path, tmp)
        sftp.posix_rename(tmp, remote_path)
        sftp.close()
        return SSHResult(True, exit_code=0, stdout=f"已推送至 {remote_path}",
                         duration_ms=int((time.perf_counter() - started) * 1000))
    except Exception as exc:  # noqa: BLE001
        return SSHResult(False, error=f"文件推送失败：{exc}",
                         duration_ms=int((time.perf_counter() - started) * 1000))
    finally:
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def download(target: Target, remote_path: str, local_path: str) -> SSHResult:
    started = time.perf_counter()
    client = None
    try:
        client = _connect(target)
        sftp = client.open_sftp()
        sftp.get(remote_path, local_path)
        sftp.close()
        return SSHResult(True, exit_code=0, stdout=f"已拉取 {remote_path}",
                         duration_ms=int((time.perf_counter() - started) * 1000))
    except Exception as exc:  # noqa: BLE001
        return SSHResult(False, error=f"文件拉取失败：{exc}",
                         duration_ms=int((time.perf_counter() - started) * 1000))
    finally:
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def iter_parallel(
    targets: Iterable[Target],
    worker: Callable[[Target], Any],
    max_concurrency: int | None = None,
) -> Iterator[tuple[Target, Any]]:
    """并发跑一批目标，**谁先跑完谁先产出**（生成器）。

    用线程池而非 asyncio：paramiko 是同步阻塞库，线程模型对它更省心；
    max_workers 限制并发，防止把一个 /24 网段一次全连满。

    为什么是生成器、而不是「先收齐再返回列表」——
    这是「逐台实时刷新」能不能成立的关键：
      · 收成列表再返回：调用方要等**最后**一台跑完才开始逐条处理，
        done=1 / done=2 这些中间进度会全部挤在最后几毫秒发出去，
        前端看到的仍是「一下子全出来」；
      · 而且只要有一台卡住（比如某台网络不通、要等满超时），
        整批的进度都会停在 0，看不出到底跑到哪了。
    """
    targets = list(targets)
    if not targets:
        return
    workers = min(max_concurrency or settings.SSH_MAX_CONCURRENCY, len(targets))
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="smartops-ssh") as pool:
        future_map = {pool.submit(worker, t): t for t in targets}
        for future in as_completed(future_map):
            target = future_map[future]
            try:
                yield target, future.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("目标 %s 执行异常：%s", target.ip, exc)
                yield target, SSHResult(False, error=f"任务异常：{exc}")


def run_parallel(
    targets: Iterable[Target],
    worker: Callable[[Target], Any],
    max_concurrency: int | None = None,
) -> list[tuple[Target, Any]]:
    """并发跑一批目标，等全部结束再返回 [(target, 结果)]。

    只适合「不关心中间过程、只要最终结果集」的场景。
    要一边出结果一边处理（推进度、写库、推送），请用 iter_parallel。
    """
    return list(iter_parallel(targets, worker, max_concurrency))
