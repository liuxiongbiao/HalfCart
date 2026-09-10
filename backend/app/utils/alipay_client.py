"""
支付宝 SDK 封装 (RSA2-SHA256 手动实现)
=======================================
统一封装支付宝 API 调用: 签名/验签/统一下单/查询/退款。

签名算法: RSA-SHA256 (RSA2)
网关:
  - 沙箱: https://openapi-sandbox.dl.alipaydev.com/gateway.do
  - 正式: https://openapi.alipay.com/gateway.do

依赖: cryptography (已存在于 requirements.txt)

参考: https://opendocs.alipay.com/open/291/105971
"""

import base64
import json
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import quote_plus, urlencode

import aiohttp
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import settings

logger = logging.getLogger(__name__)

# ── 常量 ──
_ALIPAY_CHARSET = "utf-8"
_ALIPAY_SIGN_TYPE = "RSA2"
_ALIPAY_FORMAT = "JSON"
_ALIPAY_VERSION = "1.0"

# ── API 方法名 ──
METHOD_PAGE_PAY = "alipay.trade.page.pay"
METHOD_QUERY = "alipay.trade.query"
METHOD_REFUND = "alipay.trade.refund"
METHOD_REFUND_QUERY = "alipay.trade.fastpay.refund.query"


# ══════════════════════════════════════════════
# RSA 签名 / 验签
# ══════════════════════════════════════════════


def _load_private_key():
    """加载应用私钥 (PKCS1 或 PKCS8 格式)。"""
    key_str = settings.ALIPAY_PRIVATE_KEY.get_secret_value()
    try:
        return serialization.load_pem_private_key(
            key_str.encode(_ALIPAY_CHARSET), password=None, backend=default_backend()
        )
    except Exception:
        return serialization.load_der_private_key(
            base64.b64decode(key_str), password=None, backend=default_backend()
        )


def _load_public_key():
    """加载支付宝公钥。"""
    key_str = settings.ALIPAY_PUBLIC_KEY.get_secret_value()
    return serialization.load_pem_public_key(
        key_str.encode(_ALIPAY_CHARSET), backend=default_backend()
    )


def _sign(params: dict) -> str:
    """对参数字典排序拼接后 RSA-SHA256 签名。"""
    raw = _build_query_string(params)
    private_key = _load_private_key()
    signature = private_key.sign(raw.encode(_ALIPAY_CHARSET), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode(_ALIPAY_CHARSET)


def _verify_sign(params: dict, signature: str) -> bool:
    """验证支付宝回调签名。"""
    raw = _build_query_string(params)
    public_key = _load_public_key()
    try:
        public_key.verify(
            base64.b64decode(signature),
            raw.encode(_ALIPAY_CHARSET),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def _build_query_string(params: dict) -> str:
    """
    构建待签名字符串。
    规则: 剔除 sign / sign_type → 按 key 排序 → key=value & 拼接。
    """
    filtered = {k: v for k, v in params.items() if k not in ("sign", "sign_type") and v is not None}
    sorted_pairs = sorted(filtered.items(), key=lambda x: x[0])
    return "&".join(f"{k}={v}" for k, v in sorted_pairs)


# ══════════════════════════════════════════════
# 公共参数 + 网关请求
# ══════════════════════════════════════════════


def _build_common_params(method: str, biz_content: dict) -> dict:
    """构建包含公共参数的完整请求参数字典。"""
    return {
        "app_id": settings.ALIPAY_APP_ID,
        "method": method,
        "format": _ALIPAY_FORMAT,
        "charset": _ALIPAY_CHARSET,
        "sign_type": _ALIPAY_SIGN_TYPE,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": _ALIPAY_VERSION,
        "biz_content": json.dumps(biz_content, ensure_ascii=False),
    }


async def _call_alipay(method: str, biz_content: dict, timeout: int = 15) -> dict:
    """
    通用支付宝网关调用。
    返回解析后的 JSON 响应体中的 alipay_xxx_response 节点, 或错误信息。
    """
    params = _build_common_params(method, biz_content)
    params["sign"] = _sign(params)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                settings.ALIPAY_GATEWAY,
                params=params,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                text = await resp.text()
                data = json.loads(text)

                # 提取响应节点 (key 由 method 的 '.' 替换为 '_')
                response_key = method.replace(".", "_") + "_response"
                response = data.get(response_key, {})

                if isinstance(response, dict):
                    code = response.get("code", "")
                    if code == "10000":
                        return response
                    else:
                        sub_msg = response.get("sub_msg", response.get("msg", "未知错误"))
                        raise AlipayApiException(
                            f"支付宝接口错误 [method={method}, code={code}]: {sub_msg}"
                        )
                return response
    except AlipayApiException:
        raise
    except aiohttp.ClientError as e:
        logger.error(f"支付宝网关请求异常: {e}")
        raise AlipayApiException("支付宝服务暂不可用, 请稍后重试")
    except Exception as e:
        logger.error(f"支付宝网关未知异常: {e}")
        raise AlipayApiException(f"支付宝请求失败: {e}")


# ══════════════════════════════════════════════
# 对外方法
# ══════════════════════════════════════════════


async def create_pay_url(
    out_trade_no: str,
    total_amount: str,
    subject: str,
    body: str = "",
    return_url: str = "",
    notify_url: str = "",
) -> str:
    """
    统一下单 — 返回支付宝支付页面 URL。

    使用 alipay.trade.page.pay (PC 网页支付)。

    Args:
        out_trade_no: 商户订单号
        total_amount: 金额 (元, 字符串格式, e.g. "29.90")
        subject: 商品标题
        body: 商品描述
        return_url: 同步回调地址
        notify_url: 异步通知地址

    Returns:
        完整的支付宝支付页面 URL (GET 请求即可跳转)
    """
    biz_content = {
        "out_trade_no": out_trade_no,
        "product_code": "FAST_INSTANT_TRADE_PAY",
        "total_amount": total_amount,
        "subject": subject,
        "body": body,
    }
    params = _build_common_params(METHOD_PAGE_PAY, biz_content)
    params["return_url"] = return_url or settings.ALIPAY_RETURN_URL
    params["notify_url"] = notify_url or settings.ALIPAY_NOTIFY_URL
    params["sign"] = _sign(params)

    # 拼接完整支付链接
    query = "&".join(f"{k}={quote_plus(str(v))}" for k, v in params.items())
    return f"{settings.ALIPAY_GATEWAY}?{query}"


async def query_order(out_trade_no: str) -> dict:
    """查询支付宝侧支付状态 (alipay.trade.query)。"""
    biz_content = {"out_trade_no": out_trade_no}
    return await _call_alipay(METHOD_QUERY, biz_content)


async def refund(
    out_trade_no: str,
    refund_amount: str,
    refund_reason: str = "用户申请退款",
    out_request_no: str = "",
) -> dict:
    """
    退款 (alipay.trade.refund)。

    Args:
        out_trade_no: 原商户订单号
        refund_amount: 退款金额 (元, 字符串)
        refund_reason: 退款原因
        out_request_no: 退款请求号 (幂等, 不传则自动生成)

    Returns:
        支付宝退款响应
    """
    if not out_request_no:
        from uuid import uuid4
        out_request_no = f"RF{out_trade_no[-20:]}{uuid4().hex[:6]}"

    biz_content = {
        "out_trade_no": out_trade_no,
        "refund_amount": refund_amount,
        "refund_reason": refund_reason,
        "out_request_no": out_request_no,
    }
    return await _call_alipay(METHOD_REFUND, biz_content)


async def refund_query(out_trade_no: str, out_request_no: str) -> dict:
    """退款查询 (alipay.trade.fastpay.refund.query)。"""
    biz_content = {
        "out_trade_no": out_trade_no,
        "out_request_no": out_request_no,
    }
    return await _call_alipay(METHOD_REFUND_QUERY, biz_content)


def verify_notify_sign(params: dict) -> bool:
    """验证异步回调签名 (返回 True 表示验签通过)。不修改原始字典。"""
    params_copy = dict(params)  # 创建副本，避免修改调用方原始字典
    sign = params_copy.pop("sign", "")
    params_copy.pop("sign_type", _ALIPAY_SIGN_TYPE)
    return _verify_sign(params_copy, sign)


# ══════════════════════════════════════════════
# Exceptions
# ══════════════════════════════════════════════


class AlipayApiException(Exception):
    """支付宝 API 调用异常。"""
    pass
