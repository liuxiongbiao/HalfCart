"""
纠纷数据模型 — t_disputes
=========================
底座设计 2.9 补充表: PRD 4.9 AI纠纷裁判。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.dialects.mysql import JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class Dispute(BaseModel):
    """纠纷记录表 — t_disputes。"""

    __tablename__ = "t_disputes"

    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="订单ID")
    applicant_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="发起人用户ID")
    dispute_type: Mapped[int] = mapped_column(comment="纠纷类型: 1-破损 2-货不对 3-质量问题 9-其他")
    description: Mapped[str] = mapped_column(String(2000), nullable=False, comment="纠纷描述")
    evidence_images: Mapped[str] = mapped_column(String(2000), nullable=False, server_default=text("''"), comment="凭证图片URL数组JSON")
    status: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), index=True,
        comment="0-处理中 1-已完成 2-待人工 3-已关闭"
    )
    ai_responsible_party: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("''"), comment="AI判责: creator/participant/shared"
    )
    ai_creator_ratio: Mapped[float] = mapped_column(default=0.0, server_default=text("0.0"), comment="团长责任比例")
    ai_participant_ratio: Mapped[float] = mapped_column(default=0.0, server_default=text("0.0"), comment="拼友责任比例")
    ai_confidence: Mapped[float] = mapped_column(default=0.0, server_default=text("0.0"), comment="AI置信度 0.0-1.0")
    ai_reason: Mapped[str] = mapped_column(String(2000), nullable=False, server_default=text("''"), comment="判责理由")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="处理完成时间")
