"""
半仓智拼 (HalfCart) FastAPI 应用入口
===================================
注册全局中间件、异常处理、生命周期事件、路由。
对应PRD：全栈服务入口，统一管理所有基础设施连接生命周期。

启动方式:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.schemas.common import APIResponse
from app.core.database import close_db, init_db
from app.core.es_client import close_es, init_es
from app.core.mq_client import close_mq, init_mq
from app.core.redis_client import close_redis, init_redis
from app.middleware.exception_handler import register_exception_handlers
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_log import RequestLogMiddleware

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 生命周期管理
# PRD 5.1: 服务启动时连接所有中间件，关闭时优雅释放
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    FastAPI 生命周期事件管理器。

    启动顺序（对应 docker-compose depends_on 顺序）：
      MySQL → Redis → ES → RabbitMQ

    关闭顺序：反向释放资源。
    """
    logger.info("=" * 60)
    logger.info(f"  {settings.APP_NAME} v{settings.APP_VERSION} 启动中...")
    logger.info(f"  环境: {settings.APP_ENV} | Mock支付: {settings.MOCK_PAYMENT}")
    logger.info("=" * 60)

    # ── 启动阶段 ──────────────────────────────
    try:
        await init_db()
        logger.info("✅ [1/4] MySQL 连接池初始化完成")
    except Exception as e:
        logger.critical(f"❌ MySQL 初始化失败: {e}")
        raise

    try:
        await init_redis()
        logger.info("✅ [2/4] Redis 连接池初始化完成")
    except Exception as e:
        logger.critical(f"❌ Redis 初始化失败: {e}")
        raise

    try:
        await init_es()
        logger.info("✅ [3/4] ElasticSearch 客户端初始化完成")
    except Exception as e:
        logger.warning(f"⚠️  ElasticSearch 初始化失败 (LBS搜索降级): {e}")
        # ES 不可用时服务仍可运行，仅LBS搜索降级（PRD 3.5: ES为检索副本）
        pass

    try:
        await init_mq()
        logger.info("✅ [4/4] RabbitMQ 连接初始化完成")
    except Exception as e:
        logger.warning(f"⚠️  RabbitMQ 初始化失败 (异步任务降级): {e}")
        # MQ 不可用时服务仍可运行，AI任务直接同步执行兜底（PRD 4.7降级策略）
        pass

    logger.info(f"🚀 {settings.APP_NAME} v{settings.APP_VERSION} 启动完成，等待请求...")

    yield  # ── 应用运行中 ──────────────────────

    # ── 关闭阶段 ──────────────────────────────
    logger.info("🛑 正在优雅关闭...")

    try:
        await close_mq()
    except Exception:
        pass

    try:
        await close_es()
    except Exception:
        pass

    try:
        await close_redis()
    except Exception:
        pass

    try:
        await close_db()
    except Exception:
        pass

    logger.info("👋 所有资源已释放，服务关闭")


# ──────────────────────────────────────────────
# FastAPI 应用实例
# ──────────────────────────────────────────────

# ═══ Swagger 锁图标 & 全局安全方案 ═══

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="半仓智拼 (HalfCart) — 超本地化 C2B 预售互助平台 API",
    docs_url="/docs" if settings.APP_ENV == "development" else None,
    redoc_url="/redoc" if settings.APP_ENV == "development" else None,
    lifespan=lifespan,
    # Bearer Token 安全方案 → Swagger 右上角出现 🔒 Authorize 按钮
    swagger_ui_parameters={
        "persistAuthorization": True,
    },
)

# ──────────────────────────────────────────────
# CORS 中间件
# PRD 5.2: 移动端H5跨域 (微信内置浏览器/Safari/Chrome)
# ──────────────────────────────────────────────

# [FIX P1-4] CORS: 生产环境应限制为前端域名白名单
# 开发环境允许所有来源 (本地开发标准做法)
_ALLOWED_ORIGINS = [o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.APP_ENV == "development" else _ALLOWED_ORIGINS,
    allow_credentials=False,  # JWT Bearer Token 不依赖 CORS credentials
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Trace-ID", "X-Request-ID"],
)

# ──────────────────────────────────────────────
# 自定义中间件（按注册顺序执行）
# ──────────────────────────────────────────────

app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestLogMiddleware)

# ──────────────────────────────────────────────
# 全局异常处理
# ──────────────────────────────────────────────

register_exception_handlers(app)

# ──────────────────────────────────────────────
# 健康检查
# PRD 5.4: docker-compose 健康检查端点
# ──────────────────────────────────────────────


@app.get("/health", tags=["System"])
async def health_check():
    """
    健康检查端点，供 docker-compose healthcheck 调用。
    验证数据库连接可用性，确保容器仅在服务真正就绪时标记为 healthy。
    """
    from app.core.database import async_session_factory
    from sqlalchemy import text

    db_ok = False
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    status_code = 200 if db_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "code": 0 if db_ok else 1,
            "message": "healthy" if db_ok else "database unavailable",
            "data": {
                "app": settings.APP_NAME,
                "version": settings.APP_VERSION,
                "environment": settings.APP_ENV,
                "database": "connected" if db_ok else "disconnected",
            },
        },
    )


@app.get("/health/readiness", tags=["System"])
async def readiness_check():
    """
    就绪探针 (Kubernetes Readiness Probe)。
    检查所有依赖服务的连接状态。
    """
    from app.core.database import async_session_factory
    from app.core.redis_client import get_redis

    components = {}

    # MySQL 检查
    try:
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        components["mysql"] = "healthy"
    except Exception:
        components["mysql"] = "unhealthy"

    # Redis 检查
    try:
        r = get_redis()
        await r.ping()
        components["redis"] = "healthy"
    except Exception:
        components["redis"] = "unhealthy"

    all_healthy = all(v == "healthy" for v in components.values())
    return APIResponse.success(
        data={"ready": all_healthy, "components": components},
        message="ready" if all_healthy else "not_ready"
    )


# ──────────────────────────────────────────────
# 路由注册（由 api/v1/router.py 聚合）
# ⚠️ 必须在 StaticFiles mount 之前, 否则 API 路由优先匹配
# ──────────────────────────────────────────────
from app.api.v1.router import api_router, test_api_router
app.include_router(api_router, prefix="/api/v1")
app.include_router(test_api_router, prefix="/api/v1")

# ──────────────────────────────────────────────
# 静态文件服务 (前端 H5 页面)
# ⚠️ 必须在路由注册之后, 作为兜底: API 未匹配的请求才走静态文件
# ──────────────────────────────────────────────
import os
from pathlib import Path
from starlette.responses import Response
from fastapi.staticfiles import StaticFiles


class CachedStaticFiles(StaticFiles):
    """带缓存头的静态文件服务: CSS/JS/图片缓存30天, HTML不缓存(确保实时更新)。"""
    _CACHE_EXT = {".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".woff", ".woff2", ".ttf"}
    _CACHE_SECONDS = 30 * 24 * 3600  # 30天

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        suffix = Path(path).suffix.lower()
        if suffix in self._CACHE_EXT:
            response.headers["Cache-Control"] = f"public, max-age={self._CACHE_SECONDS}, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response


_static_dir = os.path.join(os.path.dirname(__file__), "..", "frontend", "src")
if os.path.isdir(_static_dir):
    app.mount("/", CachedStaticFiles(directory=_static_dir, html=True), name="frontend")
