"""
运营后台 — 订单管理
====================
PRD 3.1 运营支撑: 全量订单查询、状态干预、强制解散。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_required
from app.core.database import get_db
from app.models.order import Order
from app.schemas.common import APIResponse, ErrorCode, PaginationResponse
from app.services import order_service
from app.utils.constants import OrderStatus

router = APIRouter()


@router.get("/orders", summary="全量订单查询")
async def list_orders(
    status: Optional[int] = Query(default=None),
    order_no: Optional[str] = Query(default=None),
    creator_id: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user: int = Depends(admin_required),
):
    stmt = select(Order)
    cnt = select(func.count(Order.id))
    if status is not None:
        stmt = stmt.where(Order.status == status)
        cnt = cnt.where(Order.status == status)
    if order_no:
        stmt = stmt.where(Order.order_no == order_no)
        cnt = cnt.where(Order.order_no == order_no)
    if creator_id:
        stmt = stmt.where(Order.creator_id == creator_id)
        cnt = cnt.where(Order.creator_id == creator_id)

    total = (await db.execute(cnt)).scalar() or 0
    stmt = stmt.order_by(Order.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    orders = (await db.execute(stmt)).scalars().all()

    items = [{
        "order_id": o.id, "order_no": o.order_no, "creator_id": o.creator_id,
        "status": o.status, "status_label": OrderStatus.label(o.status),
        "goods_name": o.goods_name, "total_people": o.total_people,
        "current_people": o.current_people, "price_per_person": str(o.price_per_person),
        "total_amount": str(o.total_amount), "is_spot": bool(o.is_spot),
        "created_at": o.created_at.isoformat() if o.created_at else None,
    } for o in orders]
    return APIResponse.success(data=PaginationResponse(total=total, page=page, page_size=page_size, items=items).model_dump())


@router.post("/orders/{order_id}/force-disband", summary="强制解散订单")
async def force_disband(order_id: int, db: AsyncSession = Depends(get_db), _user: int = Depends(admin_required)):
    """运营强制解散任意状态订单 (仅集资中调用 order_service, 其他状态标记解散)。"""
    order = await db.get(Order, order_id)
    if not order:
        return APIResponse.error(code=ErrorCode.ORDER_NOT_FOUND.value, message="订单不存在")
    if order.status == OrderStatus.GATHERING:
        await order_service.disband_order(db, order_id, operator_id=0)
        return APIResponse.success(message="已解散")
    order.status = OrderStatus.DISBANDED
    db.add(order)
    await db.commit()
    return APIResponse.success(message="已强制解散")
