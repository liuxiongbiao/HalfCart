"""
用户个人中心 API — /api/v1/user/*
====================================
PRD 3.1/3.2: 个人信息/首页/订单/流水。

所有接口通过 X-User-ID 请求头鉴权 (后续对接 JWT)。
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import mask_phone
from app.schemas.common import APIResponse, PaginationResponse
from app.schemas.user import HomePageResponse, PaymentLogItem, UpdateProfileRequest, UserProfileResponse
from app.services import order_service, user_service
from app.utils.constants import OrderStatus, PaymentLogType

router = APIRouter()
from app.api.deps import get_current_user



def _profile_dict(u) -> dict:
    return {
        "user_id": u.id,
        "phone_masked": mask_phone(u.phone) if u.phone else "****",
        "nickname": u.nickname,
        "avatar_url": u.avatar_url,
        "credit_score": u.credit_score,
        "wallet_balance": str(u.wallet_balance),
        "frozen_balance": str(u.frozen_balance),
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


# ══════════════════════════════════════════════
# 接口
# ══════════════════════════════════════════════


@router.get("/profile", summary="获取个人基础信息")
async def get_profile(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    u = await user_service.get_profile(db, user_id)
    return APIResponse.success(data=_profile_dict(u))


@router.put("/profile", summary="修改个人基础信息")
async def update_profile(
    req: UpdateProfileRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    u = await user_service.update_profile(
        db, user_id, nickname=req.nickname, avatar_url=req.avatar_url
    )
    return APIResponse.success(data=_profile_dict(u), message="个人信息已更新")


@router.get("/home", summary="个人中心首页聚合")
async def home_page(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    result = await user_service.get_home_page(db, user_id)
    return APIResponse.success(
        data={
            "profile": _profile_dict(result["user"]),
            "active_created_count": result["active_created_count"],
            "active_joined_count": result["active_joined_count"],
            "completed_count": result["completed_count"],
        }
    )


@router.get("/orders", summary="我的订单列表")
async def my_orders(
    role: str = Query(default="created", pattern="^(created|joined)$", description="角色: created-我发起的, joined-我参与的"),
    status: Optional[int] = Query(default=None, description="状态筛选"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    if role == "created":
        orders, total = await order_service.list_my_created_orders(
            db, user_id=user_id, status=status, page=page, page_size=page_size
        )
    else:
        orders, total = await order_service.list_my_joined_orders(
            db, user_id=user_id, status=status, page=page, page_size=page_size
        )

    items = []
    for o in orders:
        items.append({
            "order_id": o.id,
            "order_no": o.order_no,
            "goods_name": o.goods_name,
            "status": o.status,
            "status_label": OrderStatus.label(o.status),
            "price_per_person": str(o.price_per_person),
            "total_people": o.total_people,
            "current_people": o.current_people,
            "is_spot": bool(o.is_spot),
            "created_at": o.created_at.isoformat() if o.created_at else None,
        })

    return APIResponse.success(
        data=PaginationResponse(total=total, page=page, page_size=page_size, items=items).model_dump()
    )


@router.get("/balance-logs", summary="资金流水列表")
async def balance_logs(
    log_type: Optional[int] = Query(default=None, description="流水类型 1-9"),
    start_time: Optional[str] = Query(default=None, description="开始时间 ISO8601"),
    end_time: Optional[str] = Query(default=None, description="结束时间 ISO8601"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    st = datetime.fromisoformat(start_time) if start_time else None
    et = datetime.fromisoformat(end_time) if end_time else None

    logs, total = await user_service.list_payment_logs(
        db, user_id=user_id, log_type=log_type,
        start_time=st, end_time=et, page=page, page_size=page_size,
    )

    items = []
    for l in logs:
        items.append({
            "log_no": l.log_no,
            "log_type": l.type,
            "log_type_label": PaymentLogType.label(l.type),
            "amount": str(l.amount),
            "balance_before": str(l.balance_before),
            "balance_after": str(l.balance_after),
            "order_id": l.order_id,
            "remark": l.remark,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        })

    return APIResponse.success(
        data=PaginationResponse(total=total, page=page, page_size=page_size, items=items).model_dump()
    )
