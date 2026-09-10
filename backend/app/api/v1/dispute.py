"""
纠纷管理 API — /api/v1/disputes/*
==================================
PRD 4.9: 发起纠纷 / 查询详情 / 我的纠纷列表。
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.common import APIResponse, PaginationResponse
from app.schemas.dispute import InitiateDisputeRequest
from app.services import dispute_service
from app.utils.constants import DisputeStatus, DisputeType

router = APIRouter()
from app.api.deps import get_current_user



def _dispute_dict(d) -> dict:
    images = []
    if d.evidence_images:
        try:
            images = json.loads(d.evidence_images) if isinstance(d.evidence_images, str) else d.evidence_images
        except json.JSONDecodeError:
            images = [d.evidence_images] if d.evidence_images else []
    return {
        "dispute_id": d.id,
        "order_id": d.order_id,
        "applicant_id": d.applicant_id,
        "dispute_type": d.dispute_type,
        "dispute_type_label": DisputeType.label(d.dispute_type),
        "description": d.description,
        "evidence_images": images,
        "status": d.status,
        "status_label": {0: "处理中", 1: "已完成", 2: "待人工", 3: "已关闭"}.get(d.status, "未知"),
        "ai_responsible_party": d.ai_responsible_party,
        "ai_creator_ratio": d.ai_creator_ratio,
        "ai_participant_ratio": d.ai_participant_ratio,
        "ai_confidence": d.ai_confidence,
        "ai_reason": d.ai_reason,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
    }


# ══════════════════════════════════════════════


@router.post("", summary="发起纠纷")
async def initiate(
    req: InitiateDisputeRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """PRD 4.9: 提交纠纷 → MQ异步AI裁判。"""
    d = await dispute_service.initiate_dispute(
        db, user_id, req.order_id, req.dispute_type,
        req.description, req.evidence_images,
    )
    return APIResponse.success(
        data={"dispute_id": d.id, "status": d.status, "status_label": "处理中"},
        message="纠纷已提交, AI裁判处理中",
    )


@router.get("/my", summary="我的纠纷列表")
async def my_list(
    status: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    disputes, total = await dispute_service.list_my_disputes(
        db, user_id, status=status, page=page, page_size=page_size,
    )
    return APIResponse.success(
        data=PaginationResponse(
            total=total, page=page, page_size=page_size,
            items=[_dispute_dict(d) for d in disputes],
        ).model_dump()
    )


@router.get("/{dispute_id}", summary="纠纷详情")
async def detail(
    dispute_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    d = await dispute_service.get_dispute_detail(db, dispute_id, user_id)
    return APIResponse.success(data=_dispute_dict(d))


