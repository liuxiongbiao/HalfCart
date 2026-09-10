"""
聊天消息模型 — t_chat_messages
===============================
底座设计 2.8: 订单内1v1聊天, 阅后即焚(订单完成24h后物理销毁)。
"""

from sqlalchemy import BigInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class ChatMessage(BaseModel):
    """聊天消息表 — t_chat_messages (底座设计 2.8)。"""

    __tablename__ = "t_chat_messages"

    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="订单ID")
    sender_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="发送者ID")
    receiver_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="接收者ID")
    message_type: Mapped[int] = mapped_column(
        default=1, server_default=text("1"), comment="1-用户文本 2-系统通知"
    )
    content: Mapped[str] = mapped_column(String(2000), nullable=False, comment="消息内容")
    is_read: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="0-未读 1-已读"
    )
    business_id: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("''"), comment="幂等键: 关联业务ID"
    )
