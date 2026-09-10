"""
聊天消息 HTTP API — /api/v1/chat/*
==================================
PRD 3.1: 历史消息 / 未读统计 / 标记已读。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.chat import ChatMessageItem
from app.schemas.common import APIResponse, PaginationResponse
from app.services import chat_service
from app.utils.constants import MessageType

router = APIRouter()
from app.api.deps import get_current_user



def _msg_dict(m) -> dict:
    return {
        "message_id": m.id, "order_id": m.order_id,
        "sender_id": m.sender_id, "sender_nickname": "",
        "message_type": m.message_type,
        "message_type_label": "文本" if m.message_type == 1 else "系统通知",
        "content": m.content, "is_read": bool(m.is_read),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("/{order_id}/history", summary="聊天历史")
async def history(
    order_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    msgs, total = await chat_service.get_history(db, user_id, order_id, page=page, page_size=page_size)
    return APIResponse.success(
        data=PaginationResponse(
            total=total, page=page, page_size=page_size,
            items=[_msg_dict(m) for m in msgs],
        ).model_dump()
    )


@router.get("/unread", summary="未读消息统计")
async def unread(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    counts = await chat_service.get_unread_counts(db, user_id)
    return APIResponse.success(data=counts)


@router.post("/{order_id}/read", summary="标记已读")
async def mark_read(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    await chat_service.mark_read(db, user_id, order_id)
    return APIResponse.success(message="已标记已读")
