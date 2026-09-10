"""
微信公众号服务器校验 — GET /api/v1/wechat
==========================================
微信公众平台消息接口配置验证 (免鉴权)。

流程:
  1. 接收 signature, timestamp, nonce, echostr
  2. token + timestamp + nonce 字典序排序 → SHA1
  3. 与 signature 比对, 一致返回 echostr, 不一致 403
"""

import hashlib
import logging

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_signature(signature: str, timestamp: str, nonce: str) -> bool:
    """
    微信签名校验。

    算法: 将 token / timestamp / nonce 按字典序排序,
         拼接后 SHA1, hexdigest 与 signature 比对。
    """
    token = settings.WECHAT_MP_TOKEN
    raw = "".join(sorted([token, timestamp, nonce]))
    computed = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return computed == signature


@router.get("/wechat", summary="微信公众号服务器校验")
async def wechat_mp_verify(
    signature: str = Query(..., description="微信加密签名"),
    timestamp: str = Query(..., description="时间戳"),
    nonce: str = Query(..., description="随机数"),
    echostr: str = Query(..., description="随机字符串"),
):
    """
    微信公众平台接入校验 (GET 请求)。

    微信服务器发送 GET 请求, 携带 4 个参数。
    校验通过 → 原样返回 echostr 文本 (text/plain)。
    校验失败 → 403。
    """
    if not _verify_signature(signature, timestamp, nonce):
        logger.warning(
            f"微信签名校验失败: signature={signature} "
            f"timestamp={timestamp} nonce={nonce}"
        )
        return PlainTextResponse(content="signature verification failed", status_code=403)

    logger.info("微信服务器校验通过")
    return PlainTextResponse(content=echostr)
