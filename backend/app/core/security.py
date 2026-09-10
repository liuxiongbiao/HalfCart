"""
JWT 双Token + 手机号加密/脱敏
=============================
对应PRD:
- 4.1 用户鉴权: JWT Access Token (7天) + Refresh Token (30天)
- 5.3 数据安全: 手机号AES加密存储 + SHA256哈希索引
- 5.3 隐私保护: 手机号脱敏 13x****xxxx

底座设计:
- 用户表 phone: AES加密存储 (VARCHAR(128))
- 用户表 phone_hash: SHA256哈希 (CHAR(64)) 用于快速查找
"""

import hashlib
import logging
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

import jwt
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

from app.config import settings

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# JWT 双Token机制 (PRD 4.1)
# Access Token: 7天
# Refresh Token: 30天
# 算法: HS256
# ──────────────────────────────────────────────

import logging as _logging
from fastapi import Depends as _Depends, Header as _Header
from fastapi.security import HTTPBearer as _HTTPBearer, HTTPAuthorizationCredentials as _HTTPCredentials

from app.middleware.exception_handler import UnauthorizedException

_auth_logger = _logging.getLogger(__name__)

_JWT_SECRET = settings.JWT_SECRET_KEY.get_secret_value()
_JWT_ALGORITHM = settings.JWT_ALGORITHM

# ── Swagger 锁图标所用的安全方案 ──
bearer_scheme = _HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: _HTTPCredentials | None = _Depends(bearer_scheme),
) -> int:
    """
    标准 JWT Bearer Token 鉴权依赖。
    从 Authorization: Bearer <token> 中解析 user_id, 无效/过期/缺失 → 401。
    """
    if credentials is None:
        raise UnauthorizedException("请先登录 — 缺少 Access Token")

    token = credentials.credentials
    try:
        payload = decode_access_token(token)
        user_id = int(payload.get("sub", 0))
        if not user_id:
            raise UnauthorizedException("Token 无效: 缺少用户标识")

        # PRD 4.1: 设备版本号校验 (新登录踢掉旧设备)
        # Redis 不可用时拒绝请求 (fail-closed, 安全优先)
        try:
            token_ver = payload.get("ver", 0)
            current_ver = await _get_token_version(user_id)
            if token_ver and current_ver and int(token_ver) != current_ver:
                raise UnauthorizedException("账号在其他设备登录, 请重新登录")
        except UnauthorizedException:
            raise
        except Exception as e:
            _auth_logger.error(f"Token版本校验失败 (Redis不可用?), 拒绝请求: {e}")
            raise UnauthorizedException("鉴权服务暂不可用, 请稍后重试")

        return user_id
    except jwt.ExpiredSignatureError:
        raise UnauthorizedException("Token 已过期, 请重新登录")
    except jwt.InvalidTokenError as e:
        raise UnauthorizedException(f"Token 无效: {e}")


async def create_access_token(user_id: int, phone_hash: str) -> str:
    """
    生成 Access Token (有效期7天)。

    PRD 4.1: Access Token 用于日常接口鉴权，7天过期。

    Payload:
        - sub: 用户ID
        - phash: 手机号SHA256哈希
        - type: "access"
        - iat: 签发时间
        - exp: 过期时间 (7天)
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "phash": phone_hash,
        "type": "access",
        "ver": await _get_token_version(user_id),      # PRD 4.1: 设备版本号
        "iat": now,
        "exp": now + timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS),
        "iss": settings.JWT_TOKEN_ISSUER,
    }
    # PyJWT encode 是微秒级 CPU 操作，直接调用即可 (asyncio.to_thread 开销更大)
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


async def create_refresh_token(user_id: int, phone_hash: str) -> str:
    """
    生成 Refresh Token (有效期30天)。

    PRD 4.1: Refresh Token 用于无感刷新 Access Token，30天过期。
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "phash": phone_hash,
        "type": "refresh",
        "ver": await _get_token_version(user_id),      # PRD 4.1: 设备版本号
        "iat": now,
        "exp": now + timedelta(seconds=settings.REFRESH_TOKEN_EXPIRE_SECONDS),
        "iss": settings.JWT_TOKEN_ISSUER,
    }
    # PyJWT encode 是微秒级 CPU 操作，直接调用即可
    return jwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALGORITHM)


async def bump_token_version(user_id: int) -> int:
    """
    递增用户 Token 版本号 (后登录设备踢掉前设备 — PRD 4.1)。

    每次新设备登录时调用, 使旧设备 Token 因版本不匹配而失效。
    """
    from app.core.redis_client import get_redis
    r = get_redis()
    key = f"halfcart:token:version:{user_id}"
    new_ver = await r.incr(key)
    await r.expire(key, settings.REFRESH_TOKEN_EXPIRE_SECONDS)
    return int(new_ver)


async def _get_token_version(user_id: int) -> int:
    """获取当前 Token 版本号 (读取, 不递增)。"""
    from app.core.redis_client import get_redis
    r = get_redis()
    key = f"halfcart:token:version:{user_id}"
    ver = await r.get(key)
    return int(ver) if ver else 1


async def create_token_pair(user_id: int, phone_hash: str) -> dict[str, str]:
    """
    生成 Access + Refresh Token 对。

    Returns:
        {"access_token": str, "refresh_token": str, "token_type": "bearer"}
    """
    return {
        "access_token": await create_access_token(user_id, phone_hash),
        "refresh_token": await create_refresh_token(user_id, phone_hash),
        "token_type": "bearer",
    }


def decode_access_token(token: str) -> dict[str, Any]:
    """
    校验并解码 Access Token。

    Returns:
        解码后的 payload 字典

    Raises:
        jwt.ExpiredSignatureError: Token 已过期
        jwt.InvalidTokenError: Token 无效
    """
    payload = jwt.decode(
        token,
        _JWT_SECRET,
        algorithms=[_JWT_ALGORITHM],
        options={
            "require": ["exp", "iat", "sub", "type"],
            "verify_exp": True,
        },
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Token 类型必须是 access")
    return payload


def decode_refresh_token(token: str) -> dict[str, Any]:
    """
    校验并解码 Refresh Token。
    同时校验 type 必须为 "refresh"。
    """
    payload = jwt.decode(
        token,
        _JWT_SECRET,
        algorithms=[_JWT_ALGORITHM],
        options={
            "require": ["exp", "iat", "sub", "type"],
            "verify_exp": True,
        },
    )
    if payload.get("type") != "refresh":
        raise jwt.InvalidTokenError("Token 类型必须是 refresh")
    return payload


# ──────────────────────────────────────────────
# 手机号 AES 加密/解密 (PRD 5.3)
# 算法: AES-256-CBC (PKCS7 padding)
# 密钥: 32字节, 从配置读取
# 存储格式: base64(IV + ciphertext)
# IV: 16字节随机生成, 拼接在密文前
# ──────────────────────────────────────────────

_AES_KEY_BYTES = settings.PHONE_ENCRYPTION_KEY.get_secret_value().encode("utf-8")
_key_len = len(_AES_KEY_BYTES)
if _key_len < 16:
    raise ValueError(f"PHONE_ENCRYPTION_KEY 长度不足 ({_key_len}字节), 至少需要16字节, 建议使用 openssl rand -hex 16 生成32字节密钥")
if _key_len != 32:
    import warnings
    warnings.warn(f"PHONE_ENCRYPTION_KEY 长度非标准 ({_key_len}字节, 推荐32字节), 将自动填充/截断。生产环境建议使用 openssl rand -hex 16 生成32字节密钥", RuntimeWarning)
    # 确保密钥为32字节（不足补零，超出截断）
    _AES_KEY_BYTES = (_AES_KEY_BYTES + b"\x00" * 32)[:32]
_BLOCK_SIZE = 128  # AES 块大小 (bits)


def encrypt_phone(phone: str) -> str:
    """
    AES-256-CBC 加密手机号。

    加密流程:
        1. 生成随机 16 字节 IV
        2. PKCS7 填充明文
        3. AES-CBC 加密
        4. base64(IV + ciphertext)

    Args:
        phone: 11位明文手机号

    Returns:
        base64 编码的密文 (约44字符)
    """
    import os

    iv = os.urandom(16)
    cipher = Cipher(algorithms.AES256(_AES_KEY_BYTES), modes.CBC(iv))
    encryptor = cipher.encryptor()

    # PKCS7 填充
    padder = padding.PKCS7(_BLOCK_SIZE).padder()
    padded_data = padder.update(phone.encode("utf-8")) + padder.finalize()

    ciphertext = encryptor.update(padded_data) + encryptor.finalize()

    # IV (16 bytes) + 密文
    return b64encode(iv + ciphertext).decode("utf-8")


def decrypt_phone(encrypted_phone: str) -> str:
    """
    AES-256-CBC 解密手机号。

    Args:
        encrypted_phone: base64 编码的密文

    Returns:
        11位明文手机号
    """
    raw = b64decode(encrypted_phone.encode("utf-8"))
    iv, ciphertext = raw[:16], raw[16:]

    cipher = Cipher(algorithms.AES256(_AES_KEY_BYTES), modes.CBC(iv))
    decryptor = cipher.decryptor()

    padded_data = decryptor.update(ciphertext) + decryptor.finalize()

    # 去除 PKCS7 填充
    unpadder = padding.PKCS7(_BLOCK_SIZE).unpadder()
    plaintext = unpadder.update(padded_data) + unpadder.finalize()

    return plaintext.decode("utf-8")


# ──────────────────────────────────────────────
# 手机号 SHA256 哈希 (PRD 5.3)
# 用于快速登录查找，避免全表解密
# ──────────────────────────────────────────────

def hash_phone(phone: str) -> str:
    """
    生成手机号 SHA256 哈希。
    用于 t_users.phone_hash 字段，作为索引加速查找。

    PRD 5.3: phone_hash 为 SHA256(手机号)，存储在 CHAR(64) 字段中。

    Args:
        phone: 11位明文手机号

    Returns:
        64位十六进制 SHA256 哈希
    """
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────
# 手机号脱敏 (PRD 5.3 隐私保护)
# ──────────────────────────────────────────────

def mask_phone(phone: str) -> str:
    """
    手机号脱敏: 13812341234 → 13x****xxxx

    PRD 5.3: 非订单关联场景，手机号脱敏显示。

    Args:
        phone: 11位明文或密文手机号
               (如果是密文，先解密再脱敏)

    Returns:
        脱敏后的手机号字符串
    """
    # 如果传入的是 base64 密文，尝试解密
    plain = phone
    if len(phone) > 20 and not phone.isdigit():
        try:
            plain = decrypt_phone(phone)
        except Exception:
            pass  # 解密失败则原样脱敏

    if len(plain) >= 7:
        return f"{plain[:2]}x****{plain[-4:]}"
    return plain[:2] + "****" if len(plain) > 2 else "****"


def mask_address(address: str) -> str:
    """
    地址脱敏: 隐去具体门牌号，仅展示小区/楼宇级别。

    PRD 5.3: 地址信息隐去具体门牌号，仅展示小区/楼宇级别。

    Args:
        address: 完整地址

    Returns:
        脱敏后的地址
    """
    # 简单策略：保留前N个字符，截断到最后一个"号"或"栋"之前
    # 实际使用时可根据地址格式调整
    sensitive_suffixes = ["号", "栋", "楼", "座", "单元", "室", "户"]
    for suffix in sensitive_suffixes:
        idx = address.rfind(suffix)
        if idx > 0 and idx < len(address) - 1:
            return address[:idx + len(suffix)] + "***"
    # 未匹配到敏感后缀，截断后1/3
    cut = len(address) * 2 // 3
    return address[:cut] + "***"


# ──────────────────────────────────────────────
# 密码哈希 (使用 bcrypt — OWASP 推荐)
# ──────────────────────────────────────────────

try:
    import bcrypt as _bcrypt
    _BCRYPT_AVAILABLE = True
except ImportError:
    _BCRYPT_AVAILABLE = False
    import hashlib as _hashlib
    import hmac as _hmac


def hash_password(password: str) -> str:
    """bcrypt 密码哈希（安全加固，替代原 HMAC-SHA256）。"""
    if _BCRYPT_AVAILABLE:
        return _bcrypt.hashpw(
            password.encode("utf-8"),
            _bcrypt.gensalt()
        ).decode("utf-8")
    # Fallback: HMAC-SHA256 (仅在没有 bcrypt 时应急; 使用独立密钥, 不与 AES 加密密钥共享)
    import warnings
    warnings.warn("bcrypt 不可用, 使用不安全的 HMAC-SHA256 回退, 请立即安装 bcrypt", RuntimeWarning)
    _pwd_key = _settings.JWT_SECRET_KEY.get_secret_value().encode("utf-8")[:32].ljust(32, b"\x00")
    return _hmac.new(
        _pwd_key,
        password.encode("utf-8"),
        _hashlib.sha256,
    ).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    """校验密码哈希。支持 bcrypt 和旧版 HMAC-SHA256。"""
    if _BCRYPT_AVAILABLE and hashed.startswith("$2"):
        return _bcrypt.checkpw(
            password.encode("utf-8"),
            hashed.encode("utf-8")
        )
    # Fallback: HMAC-SHA256 comparison
    import hmac as _hmac_cmp
    return _hmac_cmp.compare_digest(hash_password(password), hashed)
