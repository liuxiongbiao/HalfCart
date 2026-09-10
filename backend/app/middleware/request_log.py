"""
请求日志中间件
=============
为每个请求注入 trace_id，记录请求路径、方法、耗时、状态码。
PRD 5.3: 全链路追踪 (trace_id 贯穿前端→后端→MQ→AI)
PRD 6.2: 埋点事件中的请求级追踪
"""

import logging
import time
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger("halfcart.access")

# 需要记录全量请求日志的路径前缀 (PRD 5.1 核心接口)
_FULL_LOG_PREFIXES = (
    "/api/v1/orders",
    "/api/v1/payment",
    "/api/v1/verification",
    "/api/v1/participants",
)

# 请求体大小记录阈值 (字节)
_BODY_LOG_THRESHOLD = 4096  # 超过4KB不记录body


class RequestLogMiddleware(BaseHTTPMiddleware):
    """
    全链路追踪 + 请求日志中间件。

    功能:
    1. 为每个请求生成唯一 trace_id，注入请求状态
    2. 在响应头中返回 X-Trace-ID
    3. 记录请求路径、方法、耗时、状态码
    4. 核心接口记录请求体/响应体概要

    trace_id 传递链路:
        前端 → Nginx (X-Request-ID) → FastAPI (X-Trace-ID)
        → MQ消息 (trace_id 字段) → AI (trace_id header)
    """

    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        # ── 生成/继承 trace_id ─────────────────
        # 优先从请求头获取（由上游或前端传入），否则生成
        trace_id = request.headers.get("X-Trace-ID") or request.headers.get(
            "X-Request-ID"
        ) or str(uuid.uuid4())

        # 注入到 request.state，供后续业务层读取
        request.state.trace_id = trace_id

        # ── 记录请求开始 ───────────────────────
        start_time = time.monotonic()
        method = request.method
        path = request.url.path

        # ── 执行请求 ───────────────────────────
        response = await call_next(request)

        # ── 记录请求结束 ───────────────────────
        elapsed_ms = round((time.monotonic() - start_time) * 1000, 2)
        status_code = response.status_code

        # 响应头注入 trace_id
        response.headers["X-Trace-ID"] = trace_id
        response.headers["X-Response-Time-Ms"] = str(elapsed_ms)

        # ── 日志输出 ───────────────────────────
        # 核心接口全量日志，其他接口采样日志
        is_core = path.startswith(_FULL_LOG_PREFIXES)
        should_log = is_core or status_code >= 400 or status_code >= 500

        if should_log:
            client_ip = (
                request.client.host if request.client else "unknown"
            )
            log_parts = [
                f"[{trace_id[:8]}]",
                f"{client_ip}",
                f"{method} {path}",
                f"→ {status_code}",
                f"({elapsed_ms}ms)",
            ]
            logger.info(" ".join(log_parts))

            # 慢请求告警 (PRD 5.1: 核心接口 ≤ 200ms)
            if is_core and elapsed_ms > 200:
                logger.warning(
                    f"[{trace_id[:8]}] 慢请求告警: {method} {path} "
                    f"耗时 {elapsed_ms}ms (阈值: 200ms)"
                )

            # 错误请求详细日志
            if status_code >= 500:
                logger.error(
                    f"[{trace_id[:8]}] 服务端错误: {method} {path} "
                    f"status={status_code} elapsed={elapsed_ms}ms"
                )

        return response
