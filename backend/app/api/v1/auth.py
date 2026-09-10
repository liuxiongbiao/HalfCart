"""
多方式登录注册 API — /api/v1/auth/*
====================================
PRD 4.1: 手机号验证码登录 + 密码登录 + 独立注册 + Token 刷新 (全部免鉴权)。
PRD 4.1 扩展: 微信扫码登录与绑定。
"""

import re
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.schemas.common import APIResponse, ErrorCode
from app.config import settings
from app.services import auth_service, sms_service, wechat_service

router = APIRouter()


# ══════════════════════════════════════════════════
# Pydantic 请求模型
# ══════════════════════════════════════════════════


class SendCodeRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11, pattern=r"^1[3-9]\d{9}$",
                       description="中国大陆11位手机号")


class LoginRequest(BaseModel):
    phone: str = Field(..., min_length=11, max_length=11, pattern=r"^1[3-9]\d{9}$")
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1, description="Refresh Token")


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=4, max_length=32, pattern=r"^[a-zA-Z0-9]+$",
                          description="账号 (字母+数字, 4-32位)")
    phone: str = Field(..., min_length=11, max_length=11, pattern=r"^1[3-9]\d{9}$")
    password: str = Field(..., min_length=8, max_length=20)
    confirm_password: str = Field(..., min_length=8, max_length=20)
    nickname: str = Field(..., min_length=2, max_length=30)
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$",
                      description="短信验证码")


class PasswordLoginRequest(BaseModel):
    account: str = Field(..., min_length=4, max_length=32,
                         description="用户名或11位手机号")
    password: str = Field(..., min_length=8, max_length=20)


def _validate_password(password: str, confirm_password: str) -> Optional[str]:
    """校验密码强度，返回错误信息或 None。"""
    if password != confirm_password:
        return "两次密码输入不一致"
    if not re.search(r"[a-zA-Z]", password):
        return "密码必须包含字母"
    if not re.search(r"\d", password):
        return "密码必须包含数字"
    return None


# ══════════════════════════════════════════════════
# 验证码登录 (已有)
# ══════════════════════════════════════════════════


@router.post("/send-login-code", summary="发送登录验证码")
async def send_login_code(req: SendCodeRequest):
    """PRD 4.1: 下发6位数字验证码, 60秒限流 + 5次/小时。"""
    try:
        result = await auth_service.send_login_code(req.phone)
        return APIResponse.success(data=result, message="验证码已发送")
    except sms_service.SmsRateLimitedException as e:
        return APIResponse.error(code=ErrorCode.AUTH_CODE_RATE_LIMITED, message=str(e))
    except sms_service.SmsSendException as e:
        return APIResponse.error(code=ErrorCode.INTERNAL_ERROR, message=str(e))


@router.post("/login-by-code", summary="验证码登录")
async def login_by_code(req: LoginRequest, request: Request):
    """PRD 4.1: 校验验证码 → 静默注册/登录 → 返回 JWT Token。"""
    # IP 维度限流：同一IP每分钟最多10次登录尝试
    client_ip = request.client.host if request.client else "unknown"
    from app.core.redis_client import get_redis
    r = get_redis()
    ip_key = f"halfcart:auth:login_rate_ip:{client_ip}"
    ip_count = await r.incr(ip_key)
    if ip_count == 1:
        await r.expire(ip_key, 60)
    if ip_count > 10:
        return APIResponse.error(
            code=ErrorCode.RATE_LIMITED,
            message="登录请求过于频繁, 请1分钟后再试",
        )
    try:
        result = await auth_service.login_by_code(req.phone, req.code)
        return APIResponse.success(data=result, message="登录成功")
    except auth_service.LoginFailedException as e:
        return APIResponse.error(code=ErrorCode.AUTH_INVALID_CODE, message=str(e))
    except auth_service.LoginLockedException as e:
        return APIResponse.error(code=ErrorCode.AUTH_ACCOUNT_BANNED, message=str(e))


# ══════════════════════════════════════════════════
# Token 刷新 (新增)
# ══════════════════════════════════════════════════


@router.post("/refresh", summary="刷新Access Token (Token Rotation)")
async def refresh_token(req: RefreshRequest):
    """PRD 4.1: 使用 Refresh Token 换取新的 Access + Refresh Token (轮换)。"""
    try:
        result = await auth_service.refresh_access_token(req.refresh_token)
        return APIResponse.success(data=result, message="Token 已刷新")
    except auth_service.LoginFailedException as e:
        return APIResponse.error(code=ErrorCode.AUTH_TOKEN_INVALID, message=str(e))
    except Exception as e:
        return APIResponse.error(code=ErrorCode.AUTH_TOKEN_EXPIRED, message=str(e))


# ══════════════════════════════════════════════════
# 独立注册 (新增)
# ══════════════════════════════════════════════════


@router.post("/register", summary="用户注册")
async def register(req: RegisterRequest):
    """PRD 4.1 扩展: 独立注册 — 账号+手机+密码, 注册即登录。"""
    # ── 密码校验 ──
    pwd_err = _validate_password(req.password, req.confirm_password)
    if pwd_err:
        return APIResponse.error(code=ErrorCode.AUTH_WEAK_PASSWORD, message=pwd_err)

    # ── SMS 验证码校验 (阿里云优先, Redis兜底) ──
    import logging
    _log = logging.getLogger(__name__)
    code_ok = False
    try:
        code_ok = await sms_service.check_code_via_aliyun(req.phone, req.code, "login")
    except Exception as e:
        _log.warning(f"阿里云验证码校验失败, fallback Redis: {e}")
        code_ok = await sms_service.verify_code(req.phone, req.code, "login")
    if not code_ok:
        return APIResponse.error(
            code=ErrorCode.AUTH_INVALID_CODE,
            message="验证码错误或已过期",
        )

    # ── 注册限流: 每手机号每分钟最多3次 ──
    from app.core.redis_client import get_redis

    r = get_redis()
    reg_key = f"halfcart:auth:register_rate:{req.phone}"
    reg_count = await r.incr(reg_key)
    if reg_count == 1:
        await r.expire(reg_key, 60)
    if reg_count > 3:
        return APIResponse.error(
            code=ErrorCode.RATE_LIMITED,
            message="注册请求过于频繁, 请1分钟后再试",
        )

    try:
        result = await auth_service.register_user(
            username=req.username,
            phone=req.phone,
            password=req.password,
            nickname=req.nickname,
        )
        return APIResponse.success(data=result, message="注册成功")
    except auth_service.RegisterDuplicateException as e:
        return APIResponse.error(code=ErrorCode.AUTH_REGISTER_DUPLICATE, message=str(e))


# ══════════════════════════════════════════════════
# 密码登录 (新增)
# ══════════════════════════════════════════════════


@router.post("/login-by-password", summary="密码登录")
async def login_by_password(req: PasswordLoginRequest, request: Request):
    """
    PRD 4.1 扩展: 统一密码登录。

    自动识别凭证类型:
      - 11位手机号 → 手机号+密码
      - 其他 → 用户名+密码
    """
    # IP 维度限流：同一IP每分钟最多10次登录尝试（防暴力破解）
    client_ip = request.client.host if request.client else "unknown"
    from app.core.redis_client import get_redis
    r = get_redis()
    ip_key = f"halfcart:auth:login_rate_ip:{client_ip}"
    ip_count = await r.incr(ip_key)
    if ip_count == 1:
        await r.expire(ip_key, 60)
    if ip_count > 10:
        return APIResponse.error(
            code=ErrorCode.RATE_LIMITED,
            message="登录请求过于频繁, 请1分钟后再试",
        )
    try:
        result = await auth_service.login_by_password(req.account, req.password)
        return APIResponse.success(data=result, message="登录成功")
    except auth_service.LoginFailedException as e:
        return APIResponse.error(code=ErrorCode.AUTH_CREDENTIALS_INVALID, message=str(e))
    except auth_service.LoginLockedException as e:
        return APIResponse.error(code=ErrorCode.AUTH_ACCOUNT_BANNED, message=str(e))


# ══════════════════════════════════════════════════
# 微信扫码登录
# ══════════════════════════════════════════════════


class WechatBindPhoneRequest(BaseModel):
    bind_token: str = Field(..., min_length=1, description="绑定临时凭证")
    phone: str = Field(..., min_length=11, max_length=11, pattern=r"^1[3-9]\d{9}$")
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$",
                      description="短信验证码")


class WechatCompleteRegisterRequest(BaseModel):
    bind_token: str = Field(..., min_length=1, description="绑定临时凭证")
    username: str = Field(..., min_length=4, max_length=32, pattern=r"^[a-zA-Z0-9]+$")
    phone: str = Field(..., min_length=11, max_length=11, pattern=r"^1[3-9]\d{9}$")
    password: str = Field(..., min_length=8, max_length=20)
    confirm_password: str = Field(..., min_length=8, max_length=20)
    nickname: str = Field(..., min_length=2, max_length=30)


@router.get("/wechat/qr", summary="获取微信授权链接")
async def wechat_qr():
    """
    生成微信公众号网页授权链接 (snsapi_userinfo)。

    前端: 调用此接口获取 auth_url → 生成二维码 / 直接跳转。
    """
    try:
        result = await wechat_service.get_wechat_qr_url()
        return APIResponse.success(data=result, message="授权链接已生成")
    except wechat_service.WechatRateLimitedException as e:
        return APIResponse.error(code=ErrorCode.RATE_LIMITED, message=str(e))


@router.get("/wechat/callback", summary="微信OAuth回调")
async def wechat_callback(code: str, state: str):
    """
    微信 OAuth 回调 (浏览器 302 跳转)。

    WeChat 服务器 → 用户浏览器 → 此接口 → 302 → 前端对应页面:

    - openid 已绑定且正常 → 302 /auth/callback?token=...&user_id=...
    - openid 未绑定 → 302 /auth/bind-phone?bind_token=...
    - 被封禁 → 302 /auth/error?code=114
    - 异常 → 302 /auth/error?code=...
    """
    from fastapi.responses import RedirectResponse
    from urllib.parse import urlencode

    frontend = settings.WECHAT_FRONTEND_URL

    try:
        result = await wechat_service.handle_wechat_callback(code, state)

        if result.get("bound"):
            # 已绑定 → 302 到前端首页/回调页, 携带 token
            params = {
                "access_token": result["access_token"],
                "refresh_token": result["refresh_token"],
                "token_type": result["token_type"],
                "user_id": str(result["user_id"]),
                "nickname": result["nickname"],
            }
            redirect_url = f"{frontend}/auth/callback?{urlencode(params)}"
            return RedirectResponse(url=redirect_url, status_code=302)

        # 未绑定 → 302 到绑定页, 携带 bind_token
        params = {
            "bind_token": result["bind_token"],
            "expires_in": str(result.get("expires_in", 300)),
        }
        redirect_url = f"{frontend}/auth/bind-phone?{urlencode(params)}"
        return RedirectResponse(url=redirect_url, status_code=302)

    except wechat_service.WechatStateExpiredException as e:
        error_params = urlencode({"code": "112", "message": str(e)})
        return RedirectResponse(
            url=f"{frontend}/auth/error?{error_params}", status_code=302
        )
    except wechat_service.WechatAccountBannedException as e:
        error_params = urlencode({"code": "114", "message": str(e)})
        return RedirectResponse(
            url=f"{frontend}/auth/error?{error_params}", status_code=302
        )
    except wechat_service.WechatAuthException as e:
        error_params = urlencode({"code": "110", "message": str(e)})
        return RedirectResponse(
            url=f"{frontend}/auth/error?{error_params}", status_code=302
        )


@router.post("/wechat/bind-phone", summary="微信绑定手机号")
async def wechat_bind_phone(req: WechatBindPhoneRequest):
    """
    微信绑定手机号 (短信验证码二次确认)。

    - 手机号已注册 → 绑定 openid + 返回登录 Token
    - 手机号未注册 → 返回 needs_register=true, 前端引导完善资料

    安全: 绑定凭证一次性使用, 5分钟过期。
    """
    try:
        result = await wechat_service.bind_phone_to_wechat(
            req.bind_token, req.phone, req.code,
        )
        if not result.get("needs_register"):
            return APIResponse.success(data=result, message="绑定成功")
        return APIResponse.success(data=result, message="请完善注册资料")
    except wechat_service.WechatRateLimitedException as e:
        return APIResponse.error(code=ErrorCode.RATE_LIMITED, message=str(e))
    except wechat_service.WechatBindTokenExpiredException as e:
        return APIResponse.error(code=ErrorCode.AUTH_WECHAT_TOKEN_EXPIRED, message=str(e))
    except wechat_service.WechatAccountBannedException as e:
        return APIResponse.error(code=ErrorCode.AUTH_WECHAT_ACCOUNT_BANNED, message=str(e))
    except wechat_service.WechatAlreadyBoundException as e:
        return APIResponse.error(code=ErrorCode.AUTH_WECHAT_ALREADY_BOUND, message=str(e))
    except wechat_service.WechatAuthException as e:
        return APIResponse.error(code=ErrorCode.AUTH_WECHAT_BIND_FAILED, message=str(e))


@router.post("/wechat/complete-register", summary="微信完善资料并注册")
async def wechat_complete_register(req: WechatCompleteRegisterRequest):
    """
    微信扫码后完善资料并完成注册绑定。

    前置条件: 已完成 /wechat/bind-phone 步骤 (phone_verified=true)。

    校验规则与独立注册完全一致: 密码含字母+数字、账号/手机号唯一。
    """
    # ── 密码校验 (与独立注册使用相同函数) ──
    pwd_err = _validate_password(req.password, req.confirm_password)
    if pwd_err:
        return APIResponse.error(code=ErrorCode.AUTH_WEAK_PASSWORD, message=pwd_err)

    try:
        result = await wechat_service.complete_wechat_register(
            bind_token=req.bind_token,
            username=req.username,
            phone=req.phone,
            password=req.password,
            nickname=req.nickname,
        )
        return APIResponse.success(data=result, message="注册成功")
    except wechat_service.WechatRateLimitedException as e:
        return APIResponse.error(code=ErrorCode.RATE_LIMITED, message=str(e))
    except wechat_service.WechatBindTokenExpiredException as e:
        return APIResponse.error(code=ErrorCode.AUTH_WECHAT_TOKEN_EXPIRED, message=str(e))
    except wechat_service.WechatAlreadyBoundException as e:
        return APIResponse.error(code=ErrorCode.AUTH_WECHAT_ALREADY_BOUND, message=str(e))
    except wechat_service.WechatRegisterDuplicateException as e:
        return APIResponse.error(code=ErrorCode.AUTH_REGISTER_DUPLICATE, message=str(e))
    except wechat_service.WechatAuthException as e:
        return APIResponse.error(code=ErrorCode.AUTH_WECHAT_BIND_FAILED, message=str(e))
