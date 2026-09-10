"""
聊天清理消费者 — 阅后即焚 (PRD 4.4)
===================================
订阅 order.chat.cleanup 队列, 消费 MQ 消息后:
  1. 查询 FINISHED 状态且完成超过24小时的订单
  2. 物理删除对应聊天消息
  3. 标记订单聊天已清理

PRD 4.4: 订单完成24小时后, 聊天记录物理销毁, 不永久留存。
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update

from app.core.mq_client import MQRoutingKey
from app.models.order import Order
from app.models.chat_message import ChatMessage
from app.tasks.consumer_base import BaseConsumer
from app.utils.constants import OrderStatus

logger = logging.getLogger(__name__)


class ChatCleanupConsumer(BaseConsumer):
    """阅后即焚消费者 — 物理删除过期聊天记录。"""

    queue_name = MQRoutingKey.ORDER_CHAT_CLEANUP

    async def handle_message(self, body: dict) -> bool:
        """
        处理清理消息。

        预期 body: {"order_id": optional, "cleanup_before_hours": 24}
        若未指定 order_id, 则清理所有符合条件的订单聊天记录。
        """
        from app.core.database import async_session_factory

        cutoff_hours = body.get("cleanup_before_hours", 24)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=cutoff_hours)
        target_order_id = body.get("order_id")

        async with async_session_factory() as session:
            try:
                # 查询符合条件的订单: FINISHED 且完成时间超过 cutoff
                stmt = select(Order.id).where(
                    Order.status == OrderStatus.FINISHED,
                    Order.finished_at < cutoff,
                )
                if target_order_id:
                    stmt = stmt.where(Order.id == target_order_id)

                result = await session.execute(stmt)
                order_ids = [row[0] for row in result.all()]

                if not order_ids:
                    logger.info(f"[ChatCleanup] 无待清理订单 (cutoff={cutoff.isoformat()})")
                    return True

                # 物理删除聊天消息
                delete_stmt = delete(ChatMessage).where(
                    ChatMessage.order_id.in_(order_ids)
                )
                del_result = await session.execute(delete_stmt)
                await session.commit()

                deleted_count = del_result.rowcount
                logger.info(
                    f"[ChatCleanup] 阅后即焚完成: "
                    f"orders={len(order_ids)} messages={deleted_count} "
                    f"cutoff={cutoff.isoformat()}"
                )

                return True

            except Exception as e:
                logger.error(f"[ChatCleanup] 清理异常: {e}", exc_info=True)
                await session.rollback()
                return False
