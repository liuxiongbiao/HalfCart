"""
聊天消息 Schema
===============
PRD 3.1: 订单内1v1聊天 + 系统通知。
"""

from typing import Optional
from pydantic import BaseModel, Field


class SendMessageRequest(BaseModel):
    """发送文本消息 (PRD 3.1)。"""
    order_id: int = Field(..., gt=0)
    content: str = Field(..., min_length=1, max_length=2000)


class ChatMessageItem(BaseModel):
    """单条消息 (PRD 3.1)。"""
    message_id: int
    order_id: int
    sender_id: int
    sender_nickname: str = ""
    message_type: int
    message_type_label: str = ""
    content: str
    is_read: bool = False
    created_at: Optional[str] = None


class UnreadCountResponse(BaseModel):
    """未读消息统计 (PRD 3.1)。"""
    total_unread: int = 0
    order_unread: dict = Field(default_factory=dict)
