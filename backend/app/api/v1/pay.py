"""
支付宝支付 API — /api/v1/pay/alipay/*
=======================================
PRD V3.2: 创建支付/异步回调/同步回调/状态查询/退款。

鉴权规则 (按端点, 非全局):
  - create / query / refund → Depends(get_current_user)
  - notify / return → 免鉴权 (支付宝服务器/浏览器回调)
"""

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.middleware.exception_handler import AppException
from app.schemas.common import APIResponse, ErrorCode
from app.services import alipay_service

router = APIRouter()


# ══════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════


class CreatePaymentRequest(BaseModel):
    order_id: int = Field(..., gt=0, description="订单ID")


class RefundRequest(BaseModel):
    order_id: int = Field(..., gt=0, description="订单ID")
    reason: str = Field(default="用户主动退款", max_length=200, description="退款原因")


# ══════════════════════════════════════════════
# 需登录接口
# ══════════════════════════════════════════════


@router.post("/create", summary="创建支付宝支付订单")
async def create_payment(
    req: CreatePaymentRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """
    PRD V3.2: 创建支付宝支付单, 返回支付链接。

    前端: GET pay_url → 跳转支付宝 → 完成支付。
    有效期 5 分钟, 期间锁定拼单名额。
    """
    try:
        result = await alipay_service.create_payment(db, user_id, req.order_id)
        return APIResponse.success(data=result, message="支付订单已创建")
    except AppException as e:
        return APIResponse.error(code=e.code, message=e.message)
    except Exception as e:
        return APIResponse.error(code=ErrorCode.INTERNAL_ERROR, message=str(e))


@router.get("/query/{out_trade_no}", summary="查询支付状态")
async def query_payment(
    out_trade_no: str,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """PRD V3.2: 查询支付状态, 本地优先。"""
    try:
        result = await alipay_service.query_payment_status(db, out_trade_no, user_id)
        return APIResponse.success(data=result, message="查询成功")
    except AppException as e:
        return APIResponse.error(code=e.code, message=e.message)
    except Exception as e:
        return APIResponse.error(code=ErrorCode.INTERNAL_ERROR, message=str(e))


@router.post("/refund", summary="申请退款")
async def apply_refund(
    req: RefundRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """
    PRD V3.2 阶梯退款:
    - GATHERING: 全额退款, 信用分-1
    - PURCHASING: 退款 75%, 违约金归团长, 信用分-3
    - DELIVERING/FINISHED: 禁止主动退款
    """
    try:
        result = await alipay_service.apply_refund(
            db, req.order_id, user_id, req.reason,
        )
        return APIResponse.success(data=result, message="退款已处理")
    except AppException as e:
        return APIResponse.error(code=e.code, message=e.message)
    except Exception as e:
        return APIResponse.error(code=ErrorCode.INTERNAL_ERROR, message=str(e))


# ══════════════════════════════════════════════
# 免鉴权接口
# ══════════════════════════════════════════════


@router.post("/notify", summary="支付宝异步回调")
async def alipay_notify(request: Request):
    """
    PRD V3.2: 支付宝 POST 异步通知 (免鉴权, 支付宝服务器调用)。

    验签 → 幂等 → 金额校验 → 资金记账 → 返回 success/fail。
    """
    from fastapi.responses import PlainTextResponse

    try:
        form_data = await request.form()
        params = {k: v for k, v in form_data.items()}
        result = await alipay_service.handle_notify(params)
        return PlainTextResponse(content=result)
    except Exception as e:
        return PlainTextResponse(content="fail")


@router.get("/return", summary="支付宝同步回调")
async def alipay_return(
    out_trade_no: str = Query(default=""),
    trade_no: str = Query(default=""),
    total_amount: str = Query(default="0"),
    sign: str = Query(default=""),
    sign_type: str = Query(default="RSA2"),
    **extra,
):
    """
    PRD V3.2: 支付宝 GET 同步回调 (免鉴权, 浏览器跳转)。

    验签 → 302 跳转前端结果页。
    """
    from fastapi.responses import RedirectResponse
    from urllib.parse import urlencode

    all_params = dict(extra)
    all_params["out_trade_no"] = out_trade_no
    all_params["trade_no"] = trade_no
    all_params["total_amount"] = total_amount
    all_params["sign"] = sign
    all_params["sign_type"] = sign_type

    result = await alipay_service.handle_return(all_params)

    if result.get("verified"):
        params = urlencode({
            "out_trade_no": out_trade_no,
            "trade_no": trade_no,
            "total_amount": total_amount,
        })
        return RedirectResponse(
            url=f"http://127.0.0.1:3000/pay/result?{params}", status_code=302
        )
    return RedirectResponse(
        url="http://127.0.0.1:3000/pay/error?message=签名校验失败", status_code=302
    )
