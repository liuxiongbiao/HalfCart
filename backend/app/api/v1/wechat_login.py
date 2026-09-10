"""
微信公众号扫码登录 — 3 API (免鉴权)
===================================
架构: 后端生成带参二维码 → 前端轮询 → 微信回调写状态 → 前端跳转

GET  /api/wechat/qrcode   → 生成 scene_id + 获取二维码 ticket + URL
POST /api/wechat/callback  → 微信 XML 回调, 解析 openid, 静默注册/登录
GET  /api/wechat/check     → 前端轮询 scene_id 状态
"""

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import aiohttp
from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy import select

from app.config import settings
from app.core.database import async_session_factory
from app.core.redis_client import get_redis
from app.core.security import create_token_pair, hash_phone
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter()

# CORS 预检专用: OPTIONS 请求直接返回 200, 不经过任何业务逻辑
@router.options("/qrcode")
@router.options("/callback")
@router.options("/check")
async def wechat_preflight():
    return Response(status_code=200)

# ── Redis Key ──
_SCENE_KEY = "halfcart:wechat:scene:{scene_id}"    # scene_id → status JSON
_ACCESS_TOKEN_KEY = "halfcart:wechat:access_token"   # 全局 access_token 缓存

_WX_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
_WX_QRCODE_URL = "https://api.weixin.qq.com/cgi-bin/qrcode/create"
_SCENE_TTL = 300  # scene 5分钟过期


# ══════════════════════════════════════════════
# Access Token 获取与缓存 (7200s有效期, 提前5分钟刷新)
# ══════════════════════════════════════════════


async def _get_access_token() -> Optional[str]:
    """获取微信公众号 access_token, Redis 缓存 7000s。"""
    r = get_redis()
    cached = await r.get(_ACCESS_TOKEN_KEY)
    if cached:
        return cached

    appid = settings.WECHAT_MP_APPID
    secret = settings.WECHAT_MP_SECRET.get_secret_value()
    if not appid or not secret:
        logger.error("WECHAT_MP_APPID 或 WECHAT_MP_SECRET 未配置")
        return None

    url = f"{_WX_TOKEN_URL}?grant_type=client_credential&appid={appid}&secret={secret}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                token = data.get("access_token")
                if token:
                    await r.setex(_ACCESS_TOKEN_KEY, 7000, token)
                    logger.info("微信 access_token 获取成功")
                    return token
                logger.error(f"获取 access_token 失败: {data}")
                return None
    except Exception as e:
        logger.error(f"获取 access_token 异常: {e}")
        return None


# ══════════════════════════════════════════════
# GET /api/wechat/qrcode — 生成带参二维码
# ══════════════════════════════════════════════


@router.get("/qrcode", summary="获取微信扫码登录二维码")
async def wechat_qrcode():
    """
    生成唯一 scene_id → 调用微信 API 创建临时二维码 → 返回二维码 URL 和 scene_id。
    前端拿到后展示二维码并开始轮询 /check。

    顶层 try/except 包裹整个逻辑, 确保任何异常都返回友好 JSON 而非 500 (避免 CORS 头丢失导致前端误报跨域错误)。
    """
    try:
        r = get_redis()
        scene_id = uuid.uuid4().hex[:16]

        # 写入 Redis: scene 初始状态 pending
        await r.setex(
            _SCENE_KEY.format(scene_id=scene_id),
            _SCENE_TTL,
            json.dumps({"status": "pending", "openid": "", "created_at": int(time.time())}),
        )

        # 获取微信临时二维码 ticket
        token = await _get_access_token()
        qr_url = ""
        qr_img = ""
        if token:
            try:
                body = {
                    "expire_seconds": _SCENE_TTL,
                    "action_name": "QR_STR_SCENE",
                    "action_info": {"scene": {"scene_str": scene_id}},
                }
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{_WX_QRCODE_URL}?access_token={token}",
                        json=body,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        data = await resp.json()
                        ticket = data.get("ticket")
                        if ticket:
                            qr_url = data.get("url", "")
                            # ticket 拼接二维码图片地址, 微信短链接是HTML页面不能直接当img src
                            qr_img = "https://mp.weixin.qq.com/cgi-bin/showqrcode?ticket=" + ticket
                            logger.info(f"二维码 ticket 获取成功: scene_id={scene_id}")
                        else:
                            logger.error(f"获取 ticket 失败: {data}")
            except Exception as e:
                logger.error(f"获取 ticket 异常: {e}")

        return {"scene_id": scene_id, "qr_url": qr_url, "qr_img": qr_img, "expires_in": _SCENE_TTL}
    except Exception as e:
        logger.error(f"获取二维码接口异常: {e}", exc_info=True)
        return {"scene_id": "", "qr_url": "", "expires_in": 0, "error": f"服务异常: {str(e)[:200]}"}


# ══════════════════════════════════════════════
# GET + POST /api/wechat/callback — 微信服务器校验 + 事件推送
# ══════════════════════════════════════════════


def _verify_wechat_sign(signature: str, timestamp: str, nonce: str) -> bool:
    """微信签名校验: token/timestamp/nonce 字典序排序 → SHA1 比对。"""
    token = settings.WECHAT_MP_TOKEN  # ⚠️ 必须与微信后台「服务器配置」中填的 Token 完全一致
    raw = "".join(sorted([token, timestamp, nonce]))
    computed = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return computed == signature


@router.get("/callback", summary="微信服务器接入验证 (GET)")
async def wechat_callback_verify(
    signature: str = Query(..., description="微信加密签名"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数"),
    echostr: str = Query(..., description="随机字符串"),
):
    """
    微信公众平台服务器配置验证。

    微信后台点击「提交」时发送 GET 请求到此 URL。
    验签通过 → 返回 echostr 纯文本 (text/plain), 绝对不能返回 JSON。
    验签失败 → 403。
    """
    if not _verify_wechat_sign(signature, timestamp, nonce):
        logger.warning(f"微信接入验签失败: signature={signature}")
        return PlainTextResponse(content="signature verification failed", status_code=403)
    logger.info("微信服务器接入验签通过")
    return PlainTextResponse(content=echostr)


@router.post("/callback", summary="微信事件推送回调 (POST)")
async def wechat_callback_event(request: Request):
    """
    接收微信服务器的 XML 事件推送 (SCAN / subscribe)。

    解析 FromUserName(openid) + EventKey(scene_id) →
    检查 openid 是否已绑定用户 → 静默注册/登录 →
    更新 Redis scene 状态为 success, 附带 Token。
    """
    import xml.etree.ElementTree as ET

    r = get_redis()
    body = await request.body()
    body_str = body.decode("utf-8", errors="ignore")

    logger.info(f"微信回调收到: {body_str[:300]}")

    try:
        root = ET.fromstring(body_str)
        msg_type = root.find("MsgType")
        event = root.find("Event")
        event_key = root.find("EventKey")
        from_user = root.find("FromUserName")  # openid

        if msg_type is None or event is None or from_user is None:
            logger.warning("微信回调 XML 缺少关键字段")
            return PlainTextResponse(content="success")  # 回复 success 避免微信重试

        msg_type = msg_type.text or ""
        event = event.text or ""
        openid = from_user.text or ""
        scene_id = (event_key.text or "").replace("qrscene_", "")  # subscribe事件有前缀

        logger.info(
            f"微信事件: msg_type={msg_type} event={event} "
            f"openid={openid[:8]}... scene_id={scene_id}"
        )

        # 仅处理关注/扫码事件
        if event not in ("SCAN", "subscribe"):
            return PlainTextResponse(content="success")

        # 更新 Redis scene 状态
        scene_key = _SCENE_KEY.format(scene_id=scene_id)
        scene_data_raw = await r.get(scene_key)
        if not scene_data_raw:
            logger.warning(f"scene_id 不存在或已过期: {scene_id}")
            return PlainTextResponse(content="success")

        # 静默注册/登录
        openid_hash = hashlib.sha256(openid.encode()).hexdigest()[:16]
        token_pair = None
        user_id = None
        nickname = ""

        async with async_session_factory() as session:
            result = await session.execute(
                select(User).where(User.wechat_openid == openid)
            )
            user = result.scalar_one_or_none()

            if user is None:
                # 静默注册: 微信扫码用户, 无手机号, 无密码
                phone_placeholder = f"wx_{openid_hash}@halfcart.local"
                user = User(
                    phone=phone_placeholder,
                    phone_hash=hashlib.sha256(phone_placeholder.encode()).hexdigest(),
                    wechat_openid=openid,
                    nickname=f"微信用户{openid[:4]}",
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
                logger.info(f"微信新用户静默注册: user_id={user.id} openid={openid[:8]}...")
            else:
                user.last_login_at = datetime.now(timezone.utc)
                session.add(user)
                await session.commit()
                logger.info(f"微信用户登录: user_id={user.id}")

            user_id = user.id
            nickname = user.nickname or ""
            token_pair = await create_token_pair(user.id, user.phone_hash)

        # 写 Redis: success + token
        await r.setex(
            scene_key,
            120,  # 扫码成功后保留2分钟供前端轮询
            json.dumps({
                "status": "success",
                "openid": openid,
                "access_token": token_pair["access_token"],
                "refresh_token": token_pair["refresh_token"],
                "token_type": token_pair["token_type"],
                "user_id": user_id,
                "nickname": nickname,
            }),
        )

        logger.info(f"微信扫码登录完成: scene_id={scene_id} user_id={user_id}")
        return PlainTextResponse(content="success")

    except ET.ParseError as e:
        logger.error(f"XML 解析失败: {e}")
        return PlainTextResponse(content="success")
    except Exception as e:
        logger.error(f"微信回调异常: {e}", exc_info=True)
        return PlainTextResponse(content="success")


# ══════════════════════════════════════════════
# GET /api/wechat/check — 前端轮询
# ══════════════════════════════════════════════


@router.get("/check", summary="轮询扫码状态")
async def wechat_check(scene_id: str = Query(..., description="场景ID")):
    """
    前端每 2 秒轮询此接口。
    status=pending → 继续等待
    status=success → 返回 token, 前端跳转
    status=expired → scene 过期
    """
    r = get_redis()
    scene_key = _SCENE_KEY.format(scene_id=scene_id)
    raw = await r.get(scene_key)

    if not raw:
        return {"status": "expired", "message": "二维码已过期, 请刷新重试"}

    data = json.loads(raw)
    return data
