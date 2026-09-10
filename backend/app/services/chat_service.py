"""
聊天与系统通知服务
=================
PRD 3.1: 订单1v1聊天 / 系统通知 / 未读计数 / 阅后即焚。

约束:
  - 会话以订单为维度, 仅参与方可进入
  - GATHERING/PURCHASING/DELIVERING 可发消息, FINISHED/DISBANDED 只读
  - 系统通知幂等 (business_id 去重)
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.exception_handler import AppException
from app.models.chat_message import ChatMessage
from app.models.order import Order
from app.models.order_participant import OrderParticipant
from app.models.user import User
from app.schemas.common import ErrorCode
from app.utils.constants import MessageType, OrderStatus

logger = logging.getLogger(__name__)

CHATABLE_STATUSES = {OrderStatus.GATHERING, OrderStatus.PURCHASING, OrderStatus.DELIVERING}


async def _get_other_party(session: AsyncSession, order_id: int, user_id: int) -> Optional[int]:
    """获取订单中另一方用户ID (团长↔拼友)。"""
    result = await session.execute(
        select(OrderParticipant).where(OrderParticipant.order_id == order_id)
    )
    participants = result.scalars().all()
    for p in participants:
        if p.user_id != user_id:
            return p.user_id
    return None


async def _check_participant(session, order_id, user_id) -> Order:
    order = await session.get(Order, order_id)
    if not order:
        raise AppException(code=ErrorCode.ORDER_NOT_FOUND, message="订单不存在")
    result = await session.execute(
        select(OrderParticipant).where(
            OrderParticipant.order_id == order_id,
            OrderParticipant.user_id == user_id,
        )
    )
    if not result.scalar_one_or_none():
        raise AppException(code=ErrorCode.ORDER_NOT_MEMBER, message="非订单参与方, 无权访问聊天")
    return order


# ══════════════════════════════════════════════
# 发送消息
# ══════════════════════════════════════════════


async def send_text_message(
    session: AsyncSession, user_id: int, order_id: int, content: str
) -> ChatMessage:
    order = await _check_participant(session, order_id, user_id)
    if order.status not in CHATABLE_STATUSES:
        raise AppException(code=ErrorCode.ORDER_STATUS_TRANSITION_DENIED, message="订单已结束, 不可发送消息")

    receiver_id = await _get_other_party(session, order_id, user_id)
    msg = ChatMessage(
        order_id=order_id, sender_id=user_id, receiver_id=receiver_id or 0,
        message_type=MessageType.TEXT, content=content,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return msg


async def send_system_notification(
    session: AsyncSession, order_id: int, user_ids: list[int],
    content: str, business_id: str = "",
) -> None:
    """
    发送系统通知 (PRD 4.2/4.5/4.6/4.9 — 由MQ消费者触发)。

    business_id: 幂等键 (如 "order:status:123:0→1"), 同一事件不重复生成通知。
    """
    if business_id:
        existing = await session.execute(
            select(ChatMessage).where(ChatMessage.business_id == business_id)
        )
        if existing.scalar_one_or_none():
            return  # 幂等

    for uid in user_ids:
        msg = ChatMessage(
            order_id=order_id, sender_id=0, receiver_id=uid,
            message_type=MessageType.SYSTEM, content=content,
            business_id=business_id,
        )
        session.add(msg)
    await session.commit()


# ══════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════


async def get_history(
    session: AsyncSession, user_id: int, order_id: int,
    page: int = 1, page_size: int = 20,
) -> tuple[list[ChatMessage], int]:
    await _check_participant(session, order_id, user_id)

    base = select(ChatMessage).where(ChatMessage.order_id == order_id).where(
        (ChatMessage.receiver_id == user_id) | (ChatMessage.sender_id == user_id)
    )
    cnt_result = await session.execute(select(func.count(ChatMessage.id)).where(
        ChatMessage.order_id == order_id
    ).where(
        (ChatMessage.receiver_id == user_id) | (ChatMessage.sender_id == user_id)
    ))
    total = cnt_result.scalar() or 0

    stmt = base.order_by(ChatMessage.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    messages = list(result.scalars().all())

    # 标记已读
    await session.execute(
        update(ChatMessage).where(
            ChatMessage.order_id == order_id,
            ChatMessage.receiver_id == user_id,
            ChatMessage.is_read == 0,
        ).values(is_read=1)
    )
    await session.commit()

    return messages, total


async def get_unread_counts(session: AsyncSession, user_id: int) -> dict:
    result = await session.execute(
        select(ChatMessage.order_id, func.count(ChatMessage.id)).where(
            ChatMessage.receiver_id == user_id,
            ChatMessage.is_read == 0,
        ).group_by(ChatMessage.order_id)
    )
    order_unread = {str(row[0]): row[1] for row in result.all()}
    return {"total_unread": sum(order_unread.values()), "order_unread": order_unread}


async def mark_read(session: AsyncSession, user_id: int, order_id: int) -> None:
    await _check_participant(session, order_id, user_id)
    await session.execute(
        update(ChatMessage).where(
            ChatMessage.order_id == order_id,
            ChatMessage.receiver_id == user_id,
            ChatMessage.is_read == 0,
        ).values(is_read=1)
    )
    await session.commit()
