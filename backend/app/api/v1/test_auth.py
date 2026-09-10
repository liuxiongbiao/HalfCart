"""
测试鉴权接口 — /api/v1/test/*
===============================
仅开发环境可用, 生产环境通过 TEST_MODE=false 关闭。
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import get_db
from app.core.security import create_access_token
from app.middleware.exception_handler import AppException
from app.models.user import User
from app.schemas.common import APIResponse, ErrorCode

router = APIRouter()


class TokenRequest(BaseModel):
    user_id: int = Field(..., gt=0, description="用户ID")


@router.post("/token", summary="[TEST] 获取Token")
async def get_test_token(
    req: TokenRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    测试专用: 根据 user_id 直接生成 JWT Access Token, 无需密码/验证码。

    生产环境 TEST_MODE=false 时自动禁用。
    """
    if not settings.TEST_MODE:
        raise AppException(
            code=ErrorCode.PERMISSION_DENIED,
            message="TEST_MODE 已关闭, 该接口不可用",
        )
    if settings.APP_ENV == "production":
        raise AppException(
            code=ErrorCode.PERMISSION_DENIED,
            message="生产环境禁止调用测试接口",
        )

    user = await db.get(User, req.user_id)
    if not user:
        raise AppException(
            code=ErrorCode.RESOURCE_NOT_FOUND,
            message=f"用户不存在: {req.user_id}",
        )

    token = await create_access_token(user.id, user.phone_hash)

    return APIResponse.success(
        data={
            "access_token": token,
            "token_type": "bearer",
            "user_id": user.id,
            "nickname": user.nickname,
        },
        message=f"Token 已生成 (TEST_MODE, user_id={user.id})",
    )
