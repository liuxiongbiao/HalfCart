# ===========================================================
# 半仓智拼 (HalfCart) 后端服务 Dockerfile
# ===========================================================
# 底座设计 1.2: Backend 服务容器
# 镜像: python:3.11-slim → 优化体积
# 运行: uvicorn (异步 ASGI)
#
# 构建:
#   docker build -t halfcart-backend:latest -f Dockerfile .
# 运行:
#   docker run -p 8000:8000 --env-file .env halfcart-backend:latest
#
# 策略: 单阶段构建 + pip install (避免 Poetry 安装耗时)
# ===========================================================

FROM python:3.11-slim

# ── 安装系统依赖 ──────────────────────────────
# libmariadb-dev-compat: aiomysql 编译依赖
# curl: 健康检查
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libmariadb-dev-compat \
    curl \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ── 创建非 root 用户 ──────────────────────────
RUN groupadd -r halfcart && useradd -r -g halfcart -d /app -s /bin/false halfcart

WORKDIR /app

# ── 安装 Python 依赖 (利用 Docker layer cache) ─
COPY requirements.txt ./
# [安全] 生产构建建议: 启用 --require-hashes 或切回 PyPI 官方源
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && rm -rf /root/.cache/pip

# ── 拷贝应用代码 ──────────────────────────────
COPY --chown=halfcart:halfcart ./app /app/app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="/app"

# ── 切换到非 root 用户 ────────────────────────
USER halfcart

# ── 暴露端口 (底座设计 1.2: Backend Port 8000) ─
EXPOSE 8000

# ── 健康检查 (底座设计 1.4) ───────────────────
HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=10s \
    CMD curl -f http://localhost:8000/health || exit 1

# ── 启动命令 ──────────────────────────────────
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "172.0.0.0/8", \
     "--log-level", "info"]
