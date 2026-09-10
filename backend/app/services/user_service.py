"""
用户个人中心服务
================
PRD 3.1/3.2: 个人信息/首页聚合/订单列表/资金流水。

约束:
  - 全部查询以当前 user_id 隔离
  - 手机号脱敏输出
  - 仅允许修改昵称/头像
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.exception_handler import AppException
from app.models.order import Order
from app.models.order_participant import OrderParticipant
from app.models.payment_log import PaymentLog
from app.models.user import User
from app.schemas.common import ErrorCode
from app.utils.constants import OrderStatus, ParticipantRole, PaymentLogType

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 个人信息
# ══════════════════════════════════════════════


async def get_profile(session: AsyncSession, user_id: int) -> User:
    """获取用户信息 (PRD 3.1)。"""
    u = await session.get(User, user_id)
    if not u:
        raise AppException(code=ErrorCode.RESOURCE_NOT_FOUND, message=f"用户不存在: {user_id}")
    return u


async def update_profile(
    session: AsyncSession,
    user_id: int,
    nickname: Optional[str] = None,
    avatar_url: Optional[str] = None,
) -> User:
    """
    修改用户信息 (仅昵称/头像 — PRD 3.1)。

    其他字段 (手机号/信用分/余额/状态) 禁止通过此接口修改。
    """
    u = await session.get(User, user_id)
    if not u:
        raise AppException(code=ErrorCode.RESOURCE_NOT_FOUND, message=f"用户不存在: {user_id}")

    if nickname is not None:
        u.nickname = nickname
    if avatar_url is not None:
        u.avatar_url = avatar_url

    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


# ══════════════════════════════════════════════
# 首页聚合
# ══════════════════════════════════════════════


async def get_home_page(session: AsyncSession, user_id: int) -> dict:
    """
    个人中心首页聚合 (PRD 3.1)。

    Returns:
        {profile, active_created_count, active_joined_count, completed_count}
    """
    user = await get_profile(session, user_id)

    # 进行中 = status < 3 (GATHERING/PURCHASING/DELIVERING), 未解散
    active_statuses = [OrderStatus.GATHERING, OrderStatus.PURCHASING, OrderStatus.DELIVERING]

    # 我发起的进行中订单
    created_active = await session.execute(
        select(func.count(Order.id)).where(
            Order.creator_id == user_id,
            Order.status.in_(active_statuses),
        )
    )
    active_created = created_active.scalar() or 0

    # 我参与的进行中订单 (role=PARTICIPANT, 排除本人发起)
    joined_active = await session.execute(
        select(func.count(OrderParticipant.id)).where(
            OrderParticipant.user_id == user_id,
            OrderParticipant.role == ParticipantRole.PARTICIPANT,
            OrderParticipant.order_id.in_(
                select(Order.id).where(
                    Order.status.in_(active_statuses),
                    Order.creator_id != user_id,
                )
            ),
        )
    )
    active_joined = joined_active.scalar() or 0

    # 已完成订单 (与本人相关, status=FINISHED)
    completed = await session.execute(
        select(func.count(Order.id)).where(
            Order.status == OrderStatus.FINISHED,
            Order.id.in_(
                select(OrderParticipant.order_id).where(
                    OrderParticipant.user_id == user_id,
                )
            ),
        )
    )
    completed_count = completed.scalar() or 0

    return {
        "user": user,
        "active_created_count": active_created,
        "active_joined_count": active_joined,
        "completed_count": completed_count,
    }


# ══════════════════════════════════════════════
# 资金流水
# ══════════════════════════════════════════════


async def list_payment_logs(
    session: AsyncSession,
    user_id: int,
    *,
    log_type: Optional[int] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[PaymentLog], int]:
    """
    资金流水列表 (PRD 3.2/4.5: 按创建时间倒序)。

    筛选维度:
      - log_type: 流水类型 (1-9, None=全部)
      - start_time/end_time: 时间范围
    """
    stmt = select(PaymentLog).where(
        PaymentLog.user_id == user_id,
        PaymentLog.status == 1,  # 仅成功流水
    )
    if log_type is not None:
        stmt = stmt.where(PaymentLog.type == log_type)
    if start_time:
        stmt = stmt.where(PaymentLog.created_at >= start_time)
    if end_time:
        stmt = stmt.where(PaymentLog.created_at <= end_time)
    stmt = stmt.order_by(PaymentLog.created_at.desc())

    # count
    count_stmt = select(func.count(PaymentLog.id)).where(
        PaymentLog.user_id == user_id,
        PaymentLog.status == 1,
    )
    if log_type is not None:
        count_stmt = count_stmt.where(PaymentLog.type == log_type)
    if start_time:
        count_stmt = count_stmt.where(PaymentLog.created_at >= start_time)
    if end_time:
        count_stmt = count_stmt.where(PaymentLog.created_at <= end_time)
    total_result = await session.execute(count_stmt)
    total = total_result.scalar() or 0

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    return list(result.scalars().all()), total
