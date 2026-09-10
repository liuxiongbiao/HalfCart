"""
运营后台 — 纠纷管理
====================
PRD 4.9: 全量纠纷查询、人工判定。
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_required
from app.core.database import get_db
from app.models.dispute import Dispute
from app.schemas.common import APIResponse, PaginationResponse
from app.utils.constants import DisputeStatus, DisputeType

router = APIRouter()


class ManualJudgeRequest(BaseModel):
    dispute_id: int = Field(..., gt=0)
    result: str = Field(..., pattern="^(creator|participant|shared)$")
    creator_ratio: float = Field(default=0.5, ge=0, le=1)
    participant_ratio: float = Field(default=0.5, ge=0, le=1)
    reason: str = Field(default="人工判定", max_length=2000)


@router.get("/disputes", summary="全量纠纷查询")
async def list_disputes(
    status: Optional[int] = Query(default=None),
    order_id: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user: int = Depends(admin_required),
):
    stmt = select(Dispute)
    cnt = select(func.count(Dispute.id))
    if status is not None:
        stmt = stmt.where(Dispute.status == status)
        cnt = cnt.where(Dispute.status == status)
    if order_id:
        stmt = stmt.where(Dispute.order_id == order_id)
        cnt = cnt.where(Dispute.order_id == order_id)

    total = (await db.execute(cnt)).scalar() or 0
    stmt = stmt.order_by(Dispute.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    disputes = (await db.execute(stmt)).scalars().all()

    items = [{
        "dispute_id": d.id, "order_id": d.order_id, "applicant_id": d.applicant_id,
        "dispute_type": d.dispute_type, "dispute_type_label": DisputeType.label(d.dispute_type),
        "description": d.description[:100],
        "status": d.status, "status_label": {0: "处理中", 1: "已完成", 2: "待人工", 3: "已关闭"}.get(d.status, ""),
        "ai_confidence": d.ai_confidence, "ai_responsible_party": d.ai_responsible_party,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    } for d in disputes]
    return APIResponse.success(data=PaginationResponse(total=total, page=page, page_size=page_size, items=items).model_dump())


@router.post("/disputes/manual-judge", summary="人工判定纠纷")
async def manual_judge(req: ManualJudgeRequest, db: AsyncSession = Depends(get_db), _user: int = Depends(admin_required)):
    d = await db.get(Dispute, req.dispute_id)
    if not d:
        return APIResponse.error(code=ErrorCode.DISPUTE_NOT_FOUND, message="纠纷不存在")
    if d.status not in (DisputeStatus.PENDING, DisputeStatus.PENDING_MANUAL):
        return APIResponse.error(code=ErrorCode.DISPUTE_NOT_ALLOWED, message="该纠纷已处理")

    d.ai_responsible_party = req.result
    d.ai_creator_ratio = req.creator_ratio
    d.ai_participant_ratio = req.participant_ratio
    d.ai_reason = req.reason
    d.ai_confidence = 1.0  # 人工判定置信度100%
    d.status = DisputeStatus.RESOLVED
    d.resolved_at = datetime.now(timezone.utc)
    db.add(d)
    await db.commit()
    return APIResponse.success(data={"dispute_id": d.id, "status": d.status, "status_label": "已完成"}, message="人工判定完成")
