"""
ES 索引同步消费者 — order.status.sync
=====================================
PRD 4.3 LBS 雷达: 订单创建/状态变更时, MySQL → ES 准实时同步。

消费队列: order.status.sync
底座设计 3.5: 异步准实时 (RabbitMQ 解耦), 延迟 < 1s
容错: 失败重试3次 → 死信队列 → 每天凌晨全量对齐兜底
"""

import logging
from typing import Any

from aio_pika.abc import AbstractIncomingMessage

from app.core.es_client import delete_order_doc, ensure_order_index, upsert_order_doc
from app.core.mq_client import MQRoutingKey
from app.tasks.consumer_base import BaseConsumer

logger = logging.getLogger(__name__)


class ESOrderSyncConsumer(BaseConsumer):
    """
    订单 ES 索引同步消费者。
    监听 order.status.sync 队列, 每次订单变更时 upsert / delete ES 文档。
    """

    queue_name = MQRoutingKey.ORDER_STATUS_SYNC

    async def handle_message(
        self, payload: dict[str, Any], message: AbstractIncomingMessage
    ) -> None:
        """
        处理 ES 同步消息。

        payload 格式:
          {
            "order_id": 123456,
            "action": "upsert" | "delete",
            "doc": { ... }    # 仅 upsert 时存在
          }
        """
        order_id = payload.get("order_id")
        action = payload.get("action", "upsert")
        doc = payload.get("doc")

        if not order_id:
            logger.error(f"[{self.queue_name}] 缺少 order_id, 无法同步")
            return

        # 确保索引存在 (幂等)
        await ensure_order_index()

        if action == "delete":
            success = await delete_order_doc(int(order_id))
            if success:
                logger.info(f"[{self.queue_name}] ES 文档已删除: order_id={order_id}")
            else:
                raise RuntimeError(f"ES 删除失败: order_id={order_id}")
        else:
            if not doc:
                logger.warning(
                    f"[{self.queue_name}] upsert 消息缺少 doc 字段, "
                    f"order_id={order_id} — 跳过"
                )
                return
            success = await upsert_order_doc(int(order_id), doc)
            if success:
                logger.info(f"[{self.queue_name}] ES 文档已同步: order_id={order_id}")
            else:
                raise RuntimeError(f"ES upsert 失败: order_id={order_id}")
