"""
微信公众号网页授权登录服务
==========================
PRD 4.1 扩展: 微信扫码 → 回调 → 绑定手机号 → 完善资料 → 登录。

授权模式: 公众号网页授权 (snsapi_userinfo)
授权地址: https://open.weixin.qq.com/connect/oauth2/authorize

流程:
  GET  /qr                 → 返回授权链接
  GET  /callback           → 微信回调, 换取 openid → 判断分支 → 302 前端
  POST /bind-phone         → 手机号绑定 (短信验证码确认)
  POST /complete-register  → 完善资料并注册 (新用户)

约束:
  - 一个微信只能绑定一个平台账号
  - 一个平台账号只能绑定一个微信
  - 绑定操作需短信验证码二次确认
  - 封禁账号禁止通过微信登录
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from urllib.parse import urlencode

import aiohttp

from app.config import settings
from app.core.database import async_session_factory
from app.core.redis_client import get_redis
from app.core.security import (
    create_token_pair,
    encrypt_phone,
    hash_password,
    hash_phone,
)
from app.models.user import User

logger = logging.getLogger(__name__)

# ── Redis Key ──
_WECHAT_STATE_KEY = "halfcart:wechat:state:{state}"
_WECHAT_BIND_TOKEN_KEY = "halfcart:wechat:bind:{token}"
_WECHAT_RATE_KEY = "halfcart:wechat:rate:{endpoint}:{identifier}"
_BIND_TOKEN_TTL = 300   # 5分钟
_RATE_LIMIT_SECONDS = 60
_MAX_REQUESTS_PER_MINUTE = 10

# ── 微信 API ──
_WX_AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"
_WX_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"


# ══════════════════════════════════════════════
# Rate Limiting Helper
# ══════════════════════════════════════════════


async def _check_rate(endpoint: str, identifier: str) -> None:
    """通用接口限流, 每分钟最多 _MAX_REQUESTS_PER_MINUTE 次。"""
    r = get_redis()
    key = _WECHAT_RATE_KEY.format(endpoint=endpoint, identifier=identifier)
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, _RATE_LIMIT_SECONDS)
    if count > _MAX_REQUESTS_PER_MINUTE:
        raise WechatRateLimitedException("请求过于频繁, 请稍后重试")


# ══════════════════════════════════════════════
# GET /auth/wechat/qr — 获取授权链接
# ══════════════════════════════════════════════


async def get_wechat_qr_url(remote_ip: str = "unknown") -> dict:
    """
    生成微信公众号网页授权链接。

    使用 open.weixin.qq.com/connect/oauth2/authorize (公众号网页授权),
    不是 qrconnect (开放平台扫码登录)。

    Returns:
        {"auth_url": str, "state": str, "expires_in": int}
    """
    await _check_rate("qr", remote_ip)

    state = uuid.uuid4().hex
    r = get_redis()

    await r.setex(
        _WECHAT_STATE_KEY.format(state=state),
        _BIND_TOKEN_TTL,
        json.dumps({
            "created_at": int(datetime.now(timezone.utc).timestamp()),
            "remote_ip": remote_ip,
        }),
    )

    redirect_uri = settings.WECHAT_REDIRECT_URI
    params = {
        "appid": settings.WECHAT_APP_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": settings.WECHAT_SCOPE,
        "state": state,
    }
    auth_url = f"{_WX_AUTHORIZE_URL}?{urlencode(params)}#wechat_redirect"

    logger.info(f"微信授权链接已生成: state={state[:8]}...")
    return {
        "auth_url": auth_url,
        "state": state,
        "expires_in": _BIND_TOKEN_TTL,
    }


# ══════════════════════════════════════════════
# GET /auth/wechat/callback — 微信回调处理
# ══════════════════════════════════════════════


async def handle_wechat_callback(code: str, state: str) -> dict:
    """
    微信 OAuth 回调处理。

    流程:
      1. 校验 state 有效 (防 CSRF)
      2. code 换取 access_token + openid
      3. 查 openid 是否已绑定 → 分支
      4. 返回结构化数据, 由路由层决定 302 跳转或 JSON 返回

    返回字段:
      - bound=true  → 登录成功, 含 token 信息
      - bound=false → 未绑定, 含 bind_token
      - error=true  → 异常
    """
    r = get_redis()

    # ── 校验 state (防 CSRF, 一次性使用) ──
    state_key = _WECHAT_STATE_KEY.format(state=state)
    if not await r.exists(state_key):
        raise WechatStateExpiredException("授权会话已过期, 请重新扫码")
    await r.delete(state_key)

    # ── code 换取 access_token + openid ──
    token_data = await _exchange_code_for_token(code)
    openid = token_data.get("openid", "")

    if not openid:
        raise WechatAuthException("获取微信 OpenID 失败, 请重新授权")

    openid_masked = openid[:8] + "..." if len(openid) > 8 else openid

    # ── 查 openid 是否已绑定 ──
    async with async_session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(User).where(User.wechat_openid == openid)
        )
        user = result.scalar_one_or_none()

        if user is not None:
            # ✅ 分支一: openid 已绑定 → 校验封禁 → 直接登录
            if user.status == 0:
                raise WechatAccountBannedException("该账号已被封禁, 请联系客服")

            user.last_login_at = datetime.now(timezone.utc)
            session.add(user)
            await session.commit()

            token_pair = await create_token_pair(user.id, user.phone_hash)
            logger.info(
                f"微信登录成功: user_id={user.id} openid={openid_masked}"
            )
            return {
                "bound": True,
                "access_token": token_pair["access_token"],
                "refresh_token": token_pair["refresh_token"],
                "token_type": token_pair["token_type"],
                "user_id": user.id,
                "nickname": user.nickname,
                "is_new_user": False,
            }

    # ⚠️ 分支二: openid 未绑定 → 签发一次性绑定凭证
    bind_token = uuid.uuid4().hex
    await r.setex(
        _WECHAT_BIND_TOKEN_KEY.format(token=bind_token),
        _BIND_TOKEN_TTL,
        json.dumps({"openid": openid}),
    )

    logger.info(
        f"微信 openid 未绑定, 签发凭证: openid={openid_masked} "
        f"bind_token={bind_token[:8]}..."
    )
    return {
        "bound": False,
        "bind_token": bind_token,
        "expires_in": _BIND_TOKEN_TTL,
        "message": "该微信未绑定账号, 请完成手机号验证",
    }


# ══════════════════════════════════════════════
# POST /auth/wechat/bind-phone — 手机号绑定
# ══════════════════════════════════════════════


async def bind_phone_to_wechat(
    bind_token: str,
    phone: str,
    verify_code: str,
) -> dict:
    """
    绑定手机号到微信账号。

    流程:
      1. 校验绑定凭证 → 取出 openid
      2. 校验短信验证码 (复用现有 SMS 服务)
      3. 查用户 by phone_hash
      4. 已注册 → 校验封禁 → 绑定 openid → 返回 Token
      5. 未注册 → 刷新凭证 TTL → 返回 needs_register=True
    """
    from app.services import sms_service

    await _check_rate("bind", phone)

    r = get_redis()

    # ── 校验绑定凭证 (一次性) ──
    token_key = _WECHAT_BIND_TOKEN_KEY.format(token=bind_token)
    raw = await r.get(token_key)
    if not raw:
        raise WechatBindTokenExpiredException("绑定凭证已过期, 请重新扫码")
    token_data = json.loads(raw)
    openid = token_data["openid"]

    # ── 校验短信验证码 ──
    try:
        ok = await sms_service.check_code_via_aliyun(phone, verify_code, "login")
    except Exception as e:
        logger.warning(f"阿里云校验失败, fallback Redis: {e}")
        ok = await sms_service.verify_code(phone, verify_code, "login")

    if not ok:
        raise WechatAuthException("短信验证码错误或已过期")

    openid_masked = openid[:8] + "..." if len(openid) > 8 else openid

    # ── 查找用户 ──
    phone_hash_val = hash_phone(phone)

    async with async_session_factory() as session:
        from sqlalchemy import select

        result = await session.execute(
            select(User).where(User.phone_hash == phone_hash_val)
        )
        user = result.scalar_one_or_none()

        if user is not None:
            # ✅ 分支A: 手机号已注册 → 校验封禁 → 绑定 openid
            if user.status == 0:
                raise WechatAccountBannedException("该账号已被封禁, 请联系客服")

            if user.wechat_openid and user.wechat_openid != openid:
                raise WechatAlreadyBoundException("该账号已绑定其他微信, 请先解绑")

            # 检查 openid 是否已被其他账号绑定 (防止并发)
            dup = await session.execute(
                select(User).where(
                    User.wechat_openid == openid,
                    User.id != user.id,
                )
            )
            if dup.scalar_one_or_none():
                raise WechatAlreadyBoundException("该微信已绑定其他账号")

            user.wechat_openid = openid
            user.last_login_at = datetime.now(timezone.utc)
            session.add(user)
            await session.commit()

            # 清除绑定凭证
            await r.delete(token_key)

            token_pair = await create_token_pair(user.id, user.phone_hash)
            logger.info(
                f"微信绑定成功: user_id={user.id} openid={openid_masked}"
            )
            return {
                "needs_register": False,
                "is_new_user": False,
                "access_token": token_pair["access_token"],
                "refresh_token": token_pair["refresh_token"],
                "token_type": token_pair["token_type"],
                "user_id": user.id,
                "nickname": user.nickname,
            }

    # ⚠️ 分支B: 手机号未注册 → 刷新凭证 TTL, 引导完善资料
    await r.setex(
        token_key,
        _BIND_TOKEN_TTL,  # 刷新 TTL, 给用户时间填写资料
        json.dumps({"openid": openid, "phone": phone, "phone_verified": True}),
    )

    logger.info(
        f"微信绑定-待注册: openid={openid_masked} phone={phone[:3]}****{phone[-4:]}"
    )
    return {
        "needs_register": True,
        "bind_token": bind_token,
        "phone": phone,
        "message": "请完善资料完成注册",
    }


# ══════════════════════════════════════════════
# POST /auth/wechat/complete-register — 完善资料
# ══════════════════════════════════════════════


async def complete_wechat_register(
    bind_token: str,
    username: str,
    phone: str,
    password: str,
    nickname: str,
) -> dict:
    """
    完善资料并完成微信注册绑定。

    流程:
      1. 校验绑定凭证 + phone 一致性 (防止伪造)
      2. 校验 username/phone/openid 唯一性
      3. 创建用户 + 绑定 openid
      4. 销毁凭证 + 返回 JWT

    与独立注册 (auth_service.register_user) 保持字段默认值一致:
      credit_score=100, wallet_balance=0.00, frozen_balance=0.00,
      role=0, status=1
    """
    await _check_rate("complete_register", phone)

    r = get_redis()

    # ── 校验绑定凭证 ──
    token_key = _WECHAT_BIND_TOKEN_KEY.format(token=bind_token)
    raw = await r.get(token_key)
    if not raw:
        raise WechatBindTokenExpiredException("绑定凭证已过期, 请重新扫码")
    token_data = json.loads(raw)
    openid = token_data["openid"]

    if not token_data.get("phone_verified"):
        raise WechatAuthException("请先完成手机号验证后再注册")

    verified_phone = token_data.get("phone", "")
    if verified_phone and verified_phone != phone:
        raise WechatAuthException("手机号与验证手机号不一致, 请重新验证")

    phone_hash_val = hash_phone(phone)
    openid_masked = openid[:8] + "..." if len(openid) > 8 else openid

    async with async_session_factory() as session:
        from sqlalchemy import select, or_

        # ── 唯一性校验 ──
        existing = await session.execute(
            select(User).where(
                or_(
                    User.username == username,
                    User.phone_hash == phone_hash_val,
                    User.wechat_openid == openid,
                )
            )
        )
        dup = existing.scalar_one_or_none()
        if dup is not None:
            if dup.wechat_openid == openid:
                raise WechatAlreadyBoundException("该微信已绑定其他账号")
            if dup.username == username:
                raise WechatRegisterDuplicateException("该账号已被注册")
            raise WechatRegisterDuplicateException("该手机号已被注册")

        # ── 创建用户 (默认值与独立注册完全一致) ──
        user = User(
            username=username,
            phone=encrypt_phone(phone),
            phone_hash=phone_hash_val,
            password_hash=hash_password(password),
            wechat_openid=openid,
            nickname=nickname,
            wallet_balance=Decimal("0.00"),
            frozen_balance=Decimal("0.00"),
            credit_score=100,
            role=0,
            status=1,
            last_login_at=datetime.now(timezone.utc),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        logger.info(
            f"微信注册绑定完成: user_id={user.id} openid={openid_masked}"
        )

    # 销毁绑定凭证
    await r.delete(token_key)

    token_pair = await create_token_pair(user.id, user.phone_hash)
    return {
        "access_token": token_pair["access_token"],
        "refresh_token": token_pair["refresh_token"],
        "token_type": token_pair["token_type"],
        "user_id": user.id,
        "nickname": user.nickname,
        "is_new_user": True,
    }


# ══════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════


async def _exchange_code_for_token(code: str) -> dict:
    """
    code 换取 access_token 和 openid。

    调用微信 /sns/oauth2/access_token API (公众号)。
    """
    params = {
        "appid": settings.WECHAT_APP_ID,
        "secret": settings.WECHAT_APP_SECRET.get_secret_value(),
        "code": code,
        "grant_type": "authorization_code",
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _WX_TOKEN_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.error(
                        f"微信 code→token HTTP错误: status={resp.status}"
                    )
                    raise WechatAuthException(
                        f"微信服务异常 (HTTP {resp.status}), 请稍后重试"
                    )
                data = await resp.json()
                if "errcode" in data and data["errcode"] != 0:
                    errcode = data.get("errcode")
                    errmsg = data.get("errmsg", "未知错误")
                    logger.error(
                        f"微信 code→token 失败: errcode={errcode} errmsg={errmsg}"
                    )
                    raise WechatAuthException(
                        f"微信授权失败 [{errcode}]: {errmsg}"
                    )
                return data
    except asyncio.TimeoutError:
        logger.error("微信 code→token 请求超时")
        raise WechatAuthException("微信服务响应超时, 请稍后重试")
    except aiohttp.ClientError as e:
        logger.error(f"微信 API 网络异常: {e}")
        raise WechatAuthException("微信服务暂时不可用, 请稍后重试")


# ══════════════════════════════════════════════
# Exceptions (细粒度错误类型, 方便前端区分处理)
# ══════════════════════════════════════════════


class WechatAuthException(Exception):
    """微信授权流程通用异常。"""
    pass


class WechatAlreadyBoundException(Exception):
    """微信/账号已绑定冲突 (可恢复, 提示解绑)。"""
    pass


class WechatStateExpiredException(Exception):
    """state 过期或无效 (需重新扫码)。"""
    pass


class WechatBindTokenExpiredException(Exception):
    """绑定凭证过期 (需重新扫码)。"""
    pass


class WechatAccountBannedException(Exception):
    """账号已被封禁。"""
    pass


class WechatRateLimitedException(Exception):
    """微信接口触发限流。"""
    pass


class WechatRegisterDuplicateException(Exception):
    """微信注册时账号/手机号重复。"""
    pass
