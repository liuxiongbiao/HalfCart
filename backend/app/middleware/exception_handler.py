"""
全局异常处理
===========
统一捕获所有未处理异常，返回标准化 APIResponse 格式。
PRD 5.4: 所有异常操作给出明确原因与下一步指引，避免用户困惑。

处理层级:
  1. 业务异常 (AppException) → 返回对应业务错误码 + 提示
  2. 参数校验异常 (RequestValidationError) → 返回字段级错误提示
  3. JWT鉴权异常 (HTTPException 401/403) → 返回鉴权错误码
  4. 系统异常 (Exception) → 返回通用错误 + 内部日志
"""

import logging
import traceback
from typing import Union

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import settings
from app.schemas.common import ErrorCode

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 业务异常基类
# ══════════════════════════════════════════════

class AppException(Exception):
    """
    业务异常基类。

    所有业务层异常应继承此类，由全局异常处理器统一捕获。

    Usage:
        raise AppException(
            code=ErrorCode.ORDER_STATUS_INVALID,
            message="拼单已满员，无法加入"
        )
    """

    def __init__(
        self,
        code: ErrorCode | int = ErrorCode.UNKNOWN_ERROR,
        message: str = "操作失败",
        status_code: int = 400,
        data: dict | None = None,
    ):
        self.code = int(code)
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


# ── 常用业务异常子类 ──


class NotFoundException(AppException):
    """资源不存在 (404)。"""
    def __init__(self, message: str = "资源不存在"):
        super().__init__(
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message=message,
            status_code=404,
        )


class PermissionDeniedException(AppException):
    """权限不足 (403)。"""
    def __init__(self, message: str = "无权限访问"):
        super().__init__(
            code=ErrorCode.PERMISSION_DENIED,
            message=message,
            status_code=403,
        )


class UnauthorizedException(AppException):
    """未认证 (401)。"""
    def __init__(self, message: str = "请先登录"):
        super().__init__(
            code=ErrorCode.AUTH_TOKEN_INVALID,
            message=message,
            status_code=401,
        )


class RateLimitedException(AppException):
    """触发限流 (429)。"""
    def __init__(self, message: str = "请求过于频繁，请稍后重试"):
        super().__init__(
            code=ErrorCode.RATE_LIMITED,
            message=message,
            status_code=429,
        )


# ══════════════════════════════════════════════
# 异常处理器注册
# ══════════════════════════════════════════════

def register_exception_handlers(app: FastAPI) -> None:
    """
    注册全局异常处理器到 FastAPI 应用。

    处理器按 specificity 从高到低顺序匹配。

    Args:
        app: FastAPI 应用实例
    """

    @app.exception_handler(AppException)
    async def handle_app_exception(
        request: Request, exc: AppException
    ) -> JSONResponse:
        """
        处理业务异常。
        返回指定业务错误码 + 用户友好提示。
        """
        logger.warning(
            f"业务异常 [{exc.code}] {exc.message} "
            f"path={request.url.path} client={request.client.host if request.client else 'unknown'}"
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "data": exc.data,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """
        处理请求参数校验异常 (Pydantic Validation)。
        将字段级错误详情转换为可读提示。
        """
        errors = exc.errors()
        # 构建字段级错误提示
        error_details = []
        for err in errors[:5]:  # 最多返回5条错误
            loc = " → ".join(str(x) for x in err.get("loc", []))
            msg = err.get("msg", "参数校验失败")
            error_details.append(f"{loc}: {msg}")

        detail = "; ".join(error_details) if error_details else "请求参数校验失败"

        logger.warning(
            f"参数校验失败 path={request.url.path} errors={detail}"
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": ErrorCode.PARAM_VALIDATION_ERROR,
                "message": detail,
                "data": {"validation_errors": errors[:5]},
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """
        处理 Starlette HTTP 异常 (401/403/404/405 等)。
        """
        code_map = {
            401: ErrorCode.AUTH_TOKEN_INVALID,
            403: ErrorCode.PERMISSION_DENIED,
            404: ErrorCode.RESOURCE_NOT_FOUND,
            405: ErrorCode.UNKNOWN_ERROR,
            429: ErrorCode.RATE_LIMITED,
        }
        error_code = code_map.get(exc.status_code, ErrorCode.UNKNOWN_ERROR)

        logger.warning(
            f"HTTP异常 status={exc.status_code} detail={exc.detail} "
            f"path={request.url.path}"
        )

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": error_code,
                "message": str(exc.detail) if exc.detail else "请求异常",
                "data": None,
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(
        request: Request, exc: Exception
    ) -> JSONResponse:
        """
        兜底异常处理器：捕获所有未被上述处理器捕获的异常。
        生产环境仅返回通用错误，开发环境返回详细堆栈。
        """
        logger.error(
            f"未捕获异常 [{type(exc).__name__}] {exc}\n"
            f"path={request.url.path}\n"
            f"{traceback.format_exc()}"
        )

        message = "服务器内部错误，请稍后重试"

        # 开发环境返回详细错误
        if settings.APP_ENV == "development":
            message = f"[DEV] {type(exc).__name__}: {str(exc)}"

        return JSONResponse(
            status_code=500,
            content={
                "code": ErrorCode.INTERNAL_ERROR,
                "message": message,
                "data": None,
            },
        )
