"""
纠纷 Schema
===========
PRD 4.9: 纠纷发起/查询/AI裁判。
"""

from typing import Optional
from pydantic import BaseModel, Field


class InitiateDisputeRequest(BaseModel):
    """发起纠纷请求 (PRD 4.9)。"""
    order_id: int = Field(..., gt=0)
    dispute_type: int = Field(..., ge=1, le=9, description="1-破损 2-货不对 3-质量 9-其他")
    description: str = Field(..., min_length=1, max_length=2000)
    evidence_images: list[str] = Field(..., min_items=1, max_items=3, description="凭证图片URL")


class DisputeDetailResponse(BaseModel):
    """纠纷详情 (PRD 4.9)。"""
    dispute_id: int = Field(...)
    order_id: int = Field(...)
    applicant_id: int = Field(...)
    dispute_type: int = Field(...)
    dispute_type_label: str = Field(default="")
    description: str = Field(default="")
    evidence_images: list = Field(default_factory=list)
    status: int = Field(...)
    status_label: str = Field(default="")
    ai_responsible_party: str = Field(default="")
    ai_creator_ratio: float = Field(default=0.0)
    ai_participant_ratio: float = Field(default=0.0)
    ai_confidence: float = Field(default=0.0)
    ai_reason: str = Field(default="")
    created_at: Optional[str] = None
    resolved_at: Optional[str] = None
