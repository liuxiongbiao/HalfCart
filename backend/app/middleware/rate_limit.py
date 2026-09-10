"""
全局限流中间件 — PRD 5.1
=========================
基于 Redis 滑动窗口, 按路径+IP 维度限流。
不同接口分级配置, 被限流请求返回 429 + 统一错误码。
"""

import logging
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.config import settings
from app.core.redis_client import rate_limit_check
from app.schemas.common import ErrorCode

logger = logging.getLogger(__name__)

# ── 分路径限流规则 (请求数/秒) ──
_RATE_LIMIT_RULES: dict[str, tuple[int, int]] = {
    "/api/v1/auth/send-login-code": (5, 60),      # 5次/60秒
    "/api/v1/auth/login-by-code": (5, 60),
    "/api/v1/auth/login-by-password": (5, 60),
    "/api/v1/auth/register": (3, 60),
    "/api/v1/pay/alipay/create": (10, 60),
    "/api/v1/pay/alipay/refund": (5, 60),
    "/api/v1/orders/": (30, 60),                   # 订单查询 30次/60秒
    "/api/v1/radar/list": (20, 10),                # 雷达列表 20次/10秒
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    """全局 Redis 滑动窗口限流中间件 (PRD 5.1)。"""

    async def dispatch(self, request: Request, call_next: Callable):
        # OPTIONS 预检请求直接放行，不干扰 CORS
        if request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        method = request.method

        # 只对写操作/高频读操作做限流
        rule = None
        for prefix, r in _RATE_LIMIT_RULES.items():
            if path.startswith(prefix):
                rule = r
                break

        if rule is None:
            return await call_next(request)

        max_req, window = rule
        client_ip = request.client.host if request.client else "unknown"
        key = f"{method}:{path}:{client_ip}"

        try:
            passed = await rate_limit_check(key, max_req, window)
        except Exception:
            # [P1-6] Redis 不可用时放行 (fail-open), 避免阻断所有请求
            # 同时记录 CRITICAL 日志供运维告警
            logger.critical(
                f"Redis不可用, 限流跳过! client={client_ip} path={path} "
                f"— 所有请求被放行, 请立即检查Redis状态"
            )
            return await call_next(request)

        if not passed:
            logger.warning(
                f"限流触发: {client_ip} {method} {path} "
                f"(limit={max_req}/{window}s)"
            )
            return JSONResponse(
                status_code=429,
                content={
                    "code": ErrorCode.RATE_LIMITED,
                    "message": "请求过于频繁, 请稍后重试",
                    "data": None,
                },
            )

        return await call_next(request)
