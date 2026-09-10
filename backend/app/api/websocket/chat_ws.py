"""
WebSocket 聊天连接管理
======================
PRD 3.1: 订单内1v1, Token鉴权, 实时推送, 内存连接池 (预留Redis扩展)。

连接: ws://host/api/v1/ws/chat/{order_id}?token=xxx
"""

import json
import logging
from typing import Dict, Set

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.services.chat_service import _check_participant, send_text_message

logger = logging.getLogger(__name__)


class ConnectionManager:
    """
    WebSocket 连接管理器 (内存实现)。

    扩展: 生产环境替换为 Redis Pub/Sub 支持多实例部署。
    """

    def __init__(self):
        # user_id → set of WebSocket connections
        self._active: Dict[int, Set[WebSocket]] = {}

    async def connect(self, ws: WebSocket, user_id: int):
        await ws.accept()
        self._active.setdefault(user_id, set()).add(ws)

    def disconnect(self, ws: WebSocket, user_id: int):
        if user_id in self._active:
            self._active[user_id].discard(ws)
            if not self._active[user_id]:
                del self._active[user_id]

    async def send_to_user(self, user_id: int, data: dict):
        """向指定用户推送消息 (预留Redis扩展位)。"""
        if user_id in self._active:
            payload = json.dumps(data, ensure_ascii=False)
            for ws in list(self._active[user_id]):
                try:
                    await ws.send_text(payload)
                except Exception:
                    self.disconnect(ws, user_id)

    async def broadcast_to_order(self, order_id: int, sender_id: int, receiver_id: int, data: dict):
        """向订单聊天房间的另一方推送。"""
        await self.send_to_user(receiver_id, data)


ws_manager = ConnectionManager()


# ══════════════════════════════════════════════
# WebSocket 端点
# ══════════════════════════════════════════════


async def chat_websocket_handler(ws: WebSocket, order_id: int, user_id: int):
    """
    WebSocket 聊天主循环 (PRD 3.1)。

    鉴权: 通过 query param token (开发阶段透传 user_id)。

    消息格式:
      发送: {"type": "text", "content": "hello"}
      接收: {"type": "text", "message_id": 1, "sender_id": 2, "content": "hello", "created_at": "..."}
      系统: {"type": "system", "content": "订单已核销", "business_id": "..."}
    """
    # 校验权限
    async with async_session_factory() as session:
        await _check_participant(session, order_id, user_id)
        other_id = await _get_other(session, order_id, user_id)

    await ws_manager.connect(ws, user_id)
    logger.info(f"WS connected: user={user_id} order={order_id}")

    try:
        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)

            if data.get("type") == "text":
                async with async_session_factory() as session:
                    msg = await send_text_message(session, user_id, order_id, data["content"])
                    outgoing = {
                        "type": "text", "message_id": msg.id, "order_id": order_id,
                        "sender_id": user_id, "content": msg.content,
                        "created_at": msg.created_at.isoformat() if msg.created_at else None,
                    }
                # 推送给对方
                if other_id:
                    await ws_manager.send_to_user(other_id, outgoing)
                # 回显给自己
                await ws.send_text(json.dumps(outgoing, ensure_ascii=False))
    except WebSocketDisconnect:
        pass
    finally:
        ws_manager.disconnect(ws, user_id)
        logger.info(f"WS disconnected: user={user_id} order={order_id}")


async def _get_other(session: AsyncSession, order_id: int, user_id: int) -> int:
    from app.models.order_participant import OrderParticipant
    from sqlalchemy import select
    result = await session.execute(
        select(OrderParticipant).where(OrderParticipant.order_id == order_id)
    )
    for p in result.scalars().all():
        if p.user_id != user_id:
            return p.user_id
    return 0
