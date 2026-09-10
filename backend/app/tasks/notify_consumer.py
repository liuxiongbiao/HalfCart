"""
订单通知消费者 — order.notify
=============================
PRD 4.2/4.4/4.5: 订单状态变更时推送 WebSocket 通知 + 短信。

消费队列: order.notify
通知类型:
  - 状态变更通知: order.status_changed → WebSocket 实时推送
  - 关键节点短信: 核销码生成 / 核销成功 → 短信通知
"""

import logging
from typing import Any

from aio_pika.abc import AbstractIncomingMessage

from app.core.mq_client import MQRoutingKey
from app.tasks.consumer_base import BaseConsumer

logger = logging.getLogger(__name__)

# 需要短信通知的关键状态变更点 (PRD 4.2/4.5)
SMS_NOTIFY_STATUSES = {
    2: "订单已进入待交接状态，请查看核销码",     # DELIVERING → 生成核销码
    3: "订单已核销完成，资金已结算至钱包",       # FINISHED → 结算完成
}


class OrderNotifyConsumer(BaseConsumer):
    """
    订单状态变更通知消费者。
    监听 order.notify 队列, 分发 WebSocket 推送 + 短信。
    """

    queue_name = MQRoutingKey.ORDER_NOTIFY

    async def handle_message(
        self, payload: dict[str, Any], message: AbstractIncomingMessage
    ) -> None:
        """
        处理通知消息。

        payload 格式:
          {
            "order_id": 123456,
            "from_status": 1,
            "to_status": 2,
            "affected_user_ids": [1001, 1002],
          }
        """
        order_id = payload.get("order_id")
        from_status = payload.get("from_status")
        to_status = payload.get("to_status")
        affected_user_ids = payload.get("affected_user_ids", [])

        logger.info(
            f"[{self.queue_name}] 订单状态变更通知: "
            f"order_id={order_id} {from_status}→{to_status} "
            f"users={affected_user_ids}"
        )

        # ── 系统通知 (chat_service 持久化) ──
        await self._send_system_notification(order_id, from_status, to_status, affected_user_ids)

        # ── WebSocket 实时推送 ─────
        await self._push_websocket(order_id, from_status, to_status, affected_user_ids)

        # ── 关键节点短信 ───────────
        if to_status in SMS_NOTIFY_STATUSES:
            await self._send_sms(order_id, to_status, affected_user_ids)

    async def _send_system_notification(
        self, order_id: int, from_status: int, to_status: int, user_ids: list[int],
    ) -> None:
        """持久化系统通知消息 (通过 chat_service, 幂等去重)。"""
        from app.core.database import async_session_factory
        from app.services.chat_service import send_system_notification
        from app.utils.constants import OrderStatus

        labels = {0: "拼单中", 1: "采购中", 2: "待交接", 3: "已完成", 4: "已解散"}
        content = f"订单状态变更: {labels.get(from_status, from_status)} → {labels.get(to_status, to_status)}"
        business_id = f"order:status:{order_id}:{from_status}→{to_status}"

        try:
            async with async_session_factory() as session:
                await send_system_notification(session, order_id, user_ids, content, business_id)
        except Exception as e:
            logger.error(f"系统通知写入失败: {e}")

    async def _push_websocket(
        self,
        order_id: int,
        from_status: int,
        to_status: int,
        user_ids: list[int],
    ) -> None:
        """
        WebSocket 实时推送状态变更。

        注意: WebSocket 连接管理器尚未实现, 当前为占位日志。
        后续在 app/api/websocket/chat_ws.py 中实现后对接。
        """
        # TODO: 对接 WebSocket 连接管理器
        # from app.api.websocket.chat_ws import ws_manager
        # await ws_manager.broadcast_to_users(
        #     user_ids,
        #     {"type": "order_status_changed", "order_id": order_id,
        #      "from_status": from_status, "to_status": to_status}
        # )
        logger.info(
            f"[{self.queue_name}] WebSocket 推送: "
            f"order_id={order_id} status {from_status}→{to_status} "
            f"→ users={user_ids} (占位, 待对接 WS 管理器)"
        )

    async def _send_sms(
        self,
        order_id: int,
        to_status: int,
        user_ids: list[int],
    ) -> None:
        """
        关键节点短信通知。
        核销码生成 (status→2) / 核销成功 (status→3) 时发送。

        注意: 短信客户端尚未实现, 当前为占位日志。
        """
        sms_text = SMS_NOTIFY_STATUSES.get(to_status, "")
        if not sms_text:
            return

        # TODO: 对接短信客户端
        # from app.core.sms_client import send_sms
        # for uid in user_ids:
        #     phone = await user_service.get_phone(uid)
        #     await send_sms(phone, sms_text)
        logger.info(
            f"[{self.queue_name}] 短信通知: "
            f"order_id={order_id} status={to_status} "
            f"text='{sms_text}' → users={user_ids} (占位, 待对接 SMS)"
        )
