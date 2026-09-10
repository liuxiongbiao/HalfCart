"""
运营后台 — 数据概览
====================
PRD 6.3: 交易大盘、用户大盘、资金看板。
"""

from fastapi import APIRouter, Depends, Header
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_required
from app.core.database import get_db
from app.models.dispute import Dispute
from app.models.order import Order
from app.models.payment_log import PaymentLog
from app.models.user import User
from app.models.withdraw import WithdrawApplication
from app.schemas.common import APIResponse
from app.utils.constants import OrderStatus, PaymentLogType, WithdrawStatus, DisputeStatus

router = APIRouter()


@router.get("/dashboard", summary="运营数据概览")
async def dashboard(db: AsyncSession = Depends(get_db), _user: int = Depends(admin_required)):
    # 用户总数
    total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0
    # 今日新增
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    new_users_today = (await db.execute(
        select(func.count(User.id)).where(User.created_at >= today)
    )).scalar() or 0

    # 订单统计
    total_orders = (await db.execute(select(func.count(Order.id)))).scalar() or 0
    active_orders = (await db.execute(
        select(func.count(Order.id)).where(Order.status.in_([0, 1, 2]))
    )).scalar() or 0
    finished_orders = (await db.execute(
        select(func.count(Order.id)).where(Order.status == OrderStatus.FINISHED)
    )).scalar() or 0

    # GMV
    settlement_sum = (await db.execute(
        select(func.sum(PaymentLog.amount)).where(
            PaymentLog.type == PaymentLogType.SETTLEMENT.value, PaymentLog.status == 1
        )
    )).scalar()
    gmv = str(settlement_sum or 0)

    # 提现待审核
    pending_withdraws = (await db.execute(
        select(func.count(WithdrawApplication.id)).where(WithdrawApplication.status == WithdrawStatus.PENDING)
    )).scalar() or 0

    # 纠纷待处理
    pending_disputes = (await db.execute(
        select(func.count(Dispute.id)).where(Dispute.status == DisputeStatus.PENDING_MANUAL)
    )).scalar() or 0

    return APIResponse.success(data={
        "users": {"total": total_users, "new_today": new_users_today},
        "orders": {"total": total_orders, "active": active_orders, "finished": finished_orders},
        "gmv": gmv,
        "pending_withdraws": pending_withdraws,
        "pending_disputes": pending_disputes,
    })
