# ============================================================
# SmartOps Assistant —— 应用镜像
#
# 构建：docker build -t smartops-assistant:0.1.0 .
# 运行：见 docker-compose.yml（app / worker / beat 共用这一个镜像）
# ============================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# ---------- 依赖层 ----------
# 单独一层：只要 requirements.txt 没变，改代码时重建不会重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- 代码层 ----------
COPY app ./app
COPY frontend ./frontend

# ---------- 运行身份与数据目录 ----------
# 不跑 root：容器逃逸时降低影响面；K8s 里也能直接过 PodSecurityPolicy
RUN mkdir -p /app/data/reports \
 && useradd -m -u 10001 -s /usr/sbin/nologin smartops \
 && chown -R smartops:smartops /app

USER smartops

EXPOSE 8100

# 用 Python 自带 urllib 做健康检查，不额外装 curl
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/api/health').read()" || exit 1

CMD ["python", "-m", "app.main"]
