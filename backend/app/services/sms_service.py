"""
阿里云短信验证码服务
===================
封装 Dypnsapi 发送验证码 + Redis 存储/校验 + 日志落库。
"""

import asyncio
import logging
import random
import time
from datetime import datetime, timezone

from app.config import settings
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

# ── Redis Key 前缀 ──
SMS_CODE_KEY = "halfcart:sms:code:{phone}:{scene}"       # 验证码
SMS_RATE_KEY = "halfcart:sms:rate:{phone}"                 # 发送频率 (60s)
SMS_HOURLY_KEY = "halfcart:sms:hourly:{phone}"             # 每小时计数


def _generate_code() -> str:
    return str(random.randint(0, 999999)).zfill(settings.SMS_CODE_LENGTH)


async def send_verify_code(phone: str, scene: str = "login") -> dict:
    """
    发送短信验证码 (PRD 4.1)。

    流程: 频率校验 → 调用阿里云 → 存Redis → 写日志。
    """
    r = get_redis()

    # ── 频率校验: 60秒内同一手机号只能发1次 ──
    rate_key = SMS_RATE_KEY.format(phone=phone)
    if await r.exists(rate_key):
        ttl = await r.ttl(rate_key)
        raise SmsRateLimitedException(f"请 {ttl} 秒后再试")
    await r.setex(rate_key, settings.SMS_CODE_RESEND_SECONDS, "1")

    # ── 频率校验: 同一手机号每小时最多5次 (PRD 4.1) ──
    hourly_key = SMS_HOURLY_KEY.format(phone=phone)
    hourly_count = await r.incr(hourly_key)
    if hourly_count == 1:
        await r.expire(hourly_key, 3600)
    if hourly_count > settings.SMS_CODE_HOURLY_LIMIT:
        raise SmsRateLimitedException(
            f"该手机号发送过于频繁, 每小时最多 {settings.SMS_CODE_HOURLY_LIMIT} 次, 请稍后再试"
        )

    # ── 生成验证码 ──
    code = _generate_code()
    t0 = time.monotonic()
    error_msg = ""
    status = 1

    # ── 调用阿里云 Dypnsapi ──
    try:
        await _call_aliyun_sms(phone, code, scene)
    except Exception as e:
        status = 0
        error_msg = str(e)[:500]
        logger.error(f"短信发送失败: phone={phone[:3]}****{phone[-4:]} scene={scene} error={e}")
        # ── 开发环境降级: 跳过阿里云, 验证码打印到日志 ──
        if settings.APP_ENV == "development":
            logger.warning(
                f"⚠️  [DEV] 阿里云发送失败, 降级为控制台输出。"
                f"验证码: {code} (手机号: {phone}, 场景: {scene})"
            )
            status = 1  # 标记为成功, 让验证码存入 Redis
            error_msg = ""
        else:
            raise SmsSendException(f"短信发送失败: {error_msg[:100]}")
    finally:
        duration_ms = int((time.monotonic() - t0) * 1000)
        await _log_sms(phone, scene, status, error_msg, duration_ms)

    # ── 验证码存 Redis (5分钟过期) ──
    code_key = SMS_CODE_KEY.format(phone=phone, scene=scene)
    await r.setex(code_key, settings.SMS_CODE_EXPIRE_SECONDS, code)

    return {"phone": phone, "scene": scene, "expires_in": settings.SMS_CODE_EXPIRE_SECONDS}


async def check_code_via_aliyun(phone: str, code: str, scene: str = "login") -> bool:
    """通过阿里云 CheckSmsVerifyCode API 校验验证码 (PRD 4.1)。"""
    from alibabacloud_dypnsapi20170525.client import Client as DypnsapiClient
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_dypnsapi20170525 import models as dypnsapi_models
    from alibabacloud_tea_util import models as util_models

    # 显式传入 AccessKey
    config = open_api_models.Config(
        access_key_id=settings.SMS_ACCESS_KEY.get_secret_value(),
        access_key_secret=settings.SMS_SECRET_KEY.get_secret_value(),
    )
    config.endpoint = settings.SMS_ENDPOINT
    client = DypnsapiClient(config)

    req = dypnsapi_models.CheckSmsVerifyCodeRequest(
        scheme_name=scene, country_code="86",
        phone_number=phone, verify_code=code, case_auth_policy=1,
    )
    runtime = util_models.RuntimeOptions()

    for attempt in range(2):
        try:
            resp = await client.check_sms_verify_code_with_options_async(req, runtime)
            body = resp.body.to_map() if resp.body else {}
            if body.get("Code") == "OK":
                return True
            logger.warning(f"阿里云验证码校验失败: {body.get('Message','')}")
            return False
        except Exception as e:
            if attempt < 1 and "timeout" in str(e).lower():
                await asyncio.sleep(0.5)
                continue
            raise

    return False


async def verify_code(phone: str, code: str, scene: str = "login") -> bool:
    """
    校验短信验证码 (PRD 4.1)。

    正确则删除 Redis 中的验证码, 错误返回 False。
    """
    r = get_redis()
    code_key = SMS_CODE_KEY.format(phone=phone, scene=scene)
    stored = await r.get(code_key)
    if stored and stored == code:
        await r.delete(code_key)
        return True
    return False


# ──────────────────────────────────────────
# 阿里云 Dypnsapi 调用
# ──────────────────────────────────────────

async def _call_aliyun_sms(phone: str, code: str, scene: str) -> None:
    """调用阿里云 Dypnsapi SendSmsVerifyCode 接口 (异步 + 重试)。"""
    from alibabacloud_dypnsapi20170525.client import Client as DypnsapiClient
    from alibabacloud_tea_openapi import models as open_api_models
    from alibabacloud_dypnsapi20170525 import models as dypnsapi_models
    from alibabacloud_tea_util import models as util_models

    # 显式传入 AccessKey, 不依赖环境变量或 CredentialClient 链
    config = open_api_models.Config(
        access_key_id=settings.SMS_ACCESS_KEY.get_secret_value(),
        access_key_secret=settings.SMS_SECRET_KEY.get_secret_value(),
    )
    config.endpoint = settings.SMS_ENDPOINT
    client = DypnsapiClient(config)

    req = dypnsapi_models.SendSmsVerifyCodeRequest(
        sign_name=settings.SMS_SIGN_NAME,
        template_code=settings.SMS_TEMPLATE_CODE,
        phone_number=phone,
        template_param=f'{{"code":"{code}","min":"{settings.SMS_CODE_EXPIRE_SECONDS // 60}"}}',
        scheme_name=scene,
        country_code="86",
        out_id=f"halfcart-{scene}-{phone}-{int(time.time())}",
        code_length=settings.SMS_CODE_LENGTH,
        valid_time=settings.SMS_CODE_EXPIRE_SECONDS,
        duplicate_policy=1,
        interval=settings.SMS_CODE_RESEND_SECONDS,
        code_type=1,
        auto_retry=1,
        return_verify_code=False,
    )

    runtime = util_models.RuntimeOptions()
    last_error = None

    for attempt in range(3):  # 重试2次
        try:
            resp = await client.send_sms_verify_code_with_options_async(req, runtime)
            body = resp.body.to_map() if resp.body else {}
            if body.get("Code") != "OK":
                raise Exception(f"阿里云返回: {body.get('Message', body.get('Code', 'unknown'))}")
            return
        except Exception as e:
            last_error = e
            if "InvalidAccessKeyId" in str(e) or "SignatureDoesNotMatch" in str(e):
                raise  # 认证错误不重试
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))

    raise last_error or Exception("短信发送失败: 未知错误")


# ──────────────────────────────────────────
# 日志持久化
# ──────────────────────────────────────────

async def _log_sms(phone: str, scene: str, status: int, error_msg: str, duration_ms: int) -> None:
    try:
        from app.core.database import async_session_factory
        from app.models.sms_log import SmsLog

        async with async_session_factory() as session:
            session.add(SmsLog(
                phone=phone, scene=scene,
                template_code=settings.SMS_TEMPLATE_CODE,
                status=status, error_msg=error_msg, duration_ms=duration_ms,
                out_id=f"halfcart-{scene}-{phone}-{int(time.time())}",
            ))
            await session.commit()
    except Exception as e:
        logger.warning(f"短信日志写入失败: {e}")


# ──────────────────────────────────────────
# 业务异常
# ──────────────────────────────────────────

class SmsSendException(Exception):
    """短信发送失败。"""
    pass


class SmsRateLimitedException(Exception):
    """短信发送频率限制。"""
    pass
