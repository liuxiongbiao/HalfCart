"""
短信测试接口 — /api/v1/test/sms/*
====================================
仅开发环境可用, TEST_MODE=false 自动禁用。
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.config import settings
from app.schemas.common import APIResponse, ErrorCode
from app.services import sms_service

router = APIRouter()


class SendSmsRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11, pattern=r"^\d{11}$")
    scene: str = Field(default="login", max_length=32)


class VerifySmsRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11, pattern=r"^\d{11}$")
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")
    scene: str = Field(default="login", max_length=32)


@router.post("/send", summary="[TEST] 发送短信验证码")
async def send_sms(req: SendSmsRequest):
    if not settings.TEST_MODE:
        return APIResponse.error(code=ErrorCode.PERMISSION_DENIED, message="仅测试环境可用")

    try:
        result = await sms_service.send_verify_code(req.phone, req.scene)
        return APIResponse.success(data=result, message="验证码已发送")
    except sms_service.SmsRateLimitedException as e:
        return APIResponse.error(code=ErrorCode.RATE_LIMITED, message=str(e))
    except sms_service.SmsSendException as e:
        return APIResponse.error(code=ErrorCode.INTERNAL_ERROR, message=str(e))


@router.post("/verify", summary="[TEST] 校验短信验证码")
async def verify_sms(req: VerifySmsRequest):
    if not settings.TEST_MODE:
        return APIResponse.error(code=ErrorCode.PERMISSION_DENIED, message="仅测试环境可用")

    ok = await sms_service.verify_code(req.phone, req.code, req.scene)
    if ok:
        return APIResponse.success(message="验证码正确")
    return APIResponse.error(code=ErrorCode.AUTH_INVALID_CODE, message="验证码错误或已过期")
