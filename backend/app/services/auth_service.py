"""
手机号验证码登录服务
====================
PRD 4.1: 短信验证码 → 校验 → 登录/静默注册 → 返回 JWT Token。
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.config import settings
from app.core.database import async_session_factory
from app.core.security import bump_token_version, create_token_pair, hash_phone
from app.models.user import User
from app.services import sms_service

logger = logging.getLogger(__name__)

# Redis key: 登录错误计数
_LOGIN_ERR_KEY = "halfcart:auth:login_err:{phone}"
_MAX_LOGIN_ERRS = 5           # 连续错误锁定阈值
_LOGIN_LOCK_MINUTES = 10       # 锁定时间


async def send_login_code(phone: str) -> dict:
    """发送登录验证码 — 60秒限流 + 单IP单日上限 (PRD 4.1)。"""
    try:
        result = await sms_service.send_verify_code(phone, "login")
        logger.info(f"登录验证码已发送: phone={phone[:3]}****{phone[-4:]}")
        return {"sent": True, "expires_in": result["expires_in"]}
    except sms_service.SmsRateLimitedException:
        raise
    except sms_service.SmsSendException:
        raise


async def login_by_code(phone: str, code: str) -> dict:
    """
    验证码登录 (PRD 4.1)。

    流程: 锁定检查 → 校验验证码 → 创建/查询用户 → 生成 JWT。
    """
    r = sms_service.get_redis()

    # ── 登录错误锁定检查 ──
    err_key = _LOGIN_ERR_KEY.format(phone=phone)
    lock_key = f"{err_key}:locked"
    if await r.exists(lock_key):
        ttl = await r.ttl(lock_key)
        raise LoginLockedException(f"登录已锁定, 请 {ttl // 60} 分钟后重试")

    # ── 校验验证码 (优先阿里云CheckSmsVerifyCode, Redis兜底) ──
    ok = await _verify_code_hybrid(phone, code)
    if not ok:
        await _incr_login_err(phone)
        remaining = _MAX_LOGIN_ERRS - int(await r.get(err_key) or 0)
        raise LoginFailedException(f"验证码错误, 还剩 {max(0, remaining)} 次机会")

    # 验证成功 → 清除错误计数
    await r.delete(err_key, lock_key)

    # ── 查找或创建用户 ──
    user_hash = hash_phone(phone)

    async with async_session_factory() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.phone_hash == user_hash))
        user = result.scalar_one_or_none()

        is_new = False
        if user is None:
            # 静默注册
            from app.core.security import encrypt_phone
            user = User(
                phone=encrypt_phone(phone),
                phone_hash=user_hash,
                nickname=f"用户{phone[-4:]}",
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
            is_new = True
            logger.info(f"新用户静默注册: user_id={user.id} phone={phone[:3]}****{phone[-4:]}")
        else:
            user.last_login_at = datetime.now(timezone.utc)
            session.add(user)
            await session.commit()

        # PRD 4.1: 新登录踢掉旧设备
        await bump_token_version(user.id)

        token_pair = await create_token_pair(user.id, user.phone_hash)

    return {
        "access_token": token_pair["access_token"],
        "refresh_token": token_pair["refresh_token"],
        "token_type": token_pair["token_type"],
        "user_id": user.id,
        "nickname": user.nickname,
        "is_new_user": is_new,
    }


# ──────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────

async def refresh_access_token(refresh_token: str) -> dict:
    """
    使用 Refresh Token 刷新 Token pair (PRD 4.1, Token Rotation)。

    校验 Refresh Token → 签发新的 Access Token + Refresh Token (轮换)。
    """
    from app.core.security import decode_refresh_token, create_token_pair

    payload = decode_refresh_token(refresh_token)
    user_id = int(payload.get("sub", 0))
    phone_hash = payload.get("phash", "")

    if not user_id:
        raise LoginFailedException("Refresh Token 无效: 缺少用户标识")

    token_pair = await create_token_pair(user_id, phone_hash)

    from app.config import settings
    return {
        "access_token": token_pair["access_token"],
        "refresh_token": token_pair["refresh_token"],
        "token_type": token_pair["token_type"],
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_SECONDS,
    }


async def register_user(
    username: str,
    phone: str,
    password: str,
    nickname: str,
) -> dict:
    """
    独立注册接口 (PRD 4.1 扩展)。

    流程: 唯一性校验 → 创建用户 → 签发 JWT。
    """
    from app.core.security import encrypt_phone, hash_password

    r = sms_service.get_redis()

    async with async_session_factory() as session:
        from sqlalchemy import select, or_

        # ── 唯一性校验 ──
        phone_hash_val = hash_phone(phone)
        existing = await session.execute(
            select(User).where(
                or_(
                    User.username == username,
                    User.phone_hash == phone_hash_val,
                )
            )
        )
        dup = existing.scalar_one_or_none()
        if dup is not None:
            if dup.username == username:
                raise RegisterDuplicateException("该账号已被注册")
            raise RegisterDuplicateException("该手机号已被注册")

        # ── 创建用户 ──
        user = User(
            username=username,
            phone=encrypt_phone(phone),
            phone_hash=phone_hash_val,
            password_hash=hash_password(password),
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
        logger.info(f"新用户注册: user_id={user.id} username={username} phone={phone[:3]}****{phone[-4:]}")

    token_pair = await create_token_pair(user.id, user.phone_hash)
    return {
        "access_token": token_pair["access_token"],
        "refresh_token": token_pair["refresh_token"],
        "token_type": token_pair["token_type"],
        "expires_in": settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        "user_id": user.id,
        "nickname": user.nickname,
        "is_new_user": True,
    }


async def login_by_password(account: str, password: str) -> dict:
    """
    统一密码登录 (PRD 4.1 扩展)。

    支持:
      - 用户名 + 密码 (account 为纯字母数字)
      - 手机号 + 密码 (account 为11位手机号)

    流程: 锁定检查 → 识别凭证类型 → 查询用户 → 密码校验 → JWT。
    """
    from app.core.security import verify_password
    import re

    r = sms_service.get_redis()

    # ── 自动识别: 手机号 or 用户名 ──
    is_phone = re.match(r"^1[3-9]\d{9}$", account)
    lookup = account  # 用于错误锁定 key

    # ── 锁定检查 ──
    err_key = _LOGIN_ERR_KEY.format(phone=lookup)
    lock_key = f"{err_key}:locked"
    if await r.exists(lock_key):
        ttl = await r.ttl(lock_key)
        raise LoginLockedException(f"登录已锁定, 请 {ttl // 60} 分钟后重试")

    async with async_session_factory() as session:
        from sqlalchemy import select

        # ── 查找用户 ──
        if is_phone:
            phone_hash_val = hash_phone(account)
            result = await session.execute(
                select(User).where(User.phone_hash == phone_hash_val)
            )
        else:
            result = await session.execute(
                select(User).where(User.username == account)
            )
        user = result.scalar_one_or_none()

        if user is None:
            await _incr_login_err(lookup)
            remaining = _MAX_LOGIN_ERRS - int(await r.get(err_key) or 0)
            raise LoginFailedException(f"账号或密码错误, 还剩 {max(0, remaining)} 次机会")

        # ── 封禁检查 ──
        if user.status == 0:
            raise LoginLockedException("该账号已被封禁, 请联系客服")

        # ── 密码校验 (恒定时间) ──
        if not user.password_hash:
            # 验证码注册用户未设密码
            await _incr_login_err(lookup)
            remaining = _MAX_LOGIN_ERRS - int(await r.get(err_key) or 0)
            raise LoginFailedException(
                f"该账号未设置密码, 请使用验证码登录。还剩 {max(0, remaining)} 次机会"
            )

        if not verify_password(password, user.password_hash):
            await _incr_login_err(lookup)
            remaining = _MAX_LOGIN_ERRS - int(await r.get(err_key) or 0)
            raise LoginFailedException(f"账号或密码错误, 还剩 {max(0, remaining)} 次机会")

        # 登录成功 → 清除错误计数
        await r.delete(err_key, lock_key)

        user.last_login_at = datetime.now(timezone.utc)
        session.add(user)
        await session.commit()

        token_pair = await create_token_pair(user.id, user.phone_hash)

    return {
        "access_token": token_pair["access_token"],
        "refresh_token": token_pair["refresh_token"],
        "token_type": token_pair["token_type"],
        "user_id": user.id,
        "nickname": user.nickname,
        "is_new_user": False,
    }


async def _verify_code_hybrid(phone: str, code: str) -> bool:
    """校验验证码: 优先阿里云 API, Redis 兜底。"""
    try:
        return await sms_service.check_code_via_aliyun(phone, code, "login")
    except Exception as e:
        logger.warning(f"阿里云校验失败, fallback Redis: {e}")
        return await sms_service.verify_code(phone, code, "login")


async def _incr_login_err(phone: str) -> None:
    """累加登录错误计数, 达到5次锁定10分钟。"""
    r = sms_service.get_redis()
    err_key = _LOGIN_ERR_KEY.format(phone=phone)
    lock_key = f"{err_key}:locked"
    count = await r.incr(err_key)
    await r.expire(err_key, 600)
    if count >= _MAX_LOGIN_ERRS:
        await r.setex(lock_key, _LOGIN_LOCK_MINUTES * 60, "1")
        logger.warning(f"登录锁定: phone={phone[:3]}****{phone[-4:]} 错误{count}次")


# ──────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────

class LoginFailedException(Exception):
    pass

class LoginLockedException(Exception):
    pass

class RegisterDuplicateException(Exception):
    pass
