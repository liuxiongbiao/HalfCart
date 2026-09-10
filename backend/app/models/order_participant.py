"""
订单参与人模型 — t_order_participants
=====================================
底座设计 2.4: 记录团长/拼友在订单中的角色与支付状态。
"""

from decimal import Decimal
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class OrderParticipant(BaseModel):
    """订单参与人表 — t_order_participants (底座设计 2.4)。"""

    __tablename__ = "t_order_participants"

    order_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="订单ID")
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="用户ID")
    role: Mapped[int] = mapped_column(comment="1-团长 2-拼友")
    pay_status: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="0-待支付 1-已支付 2-已退款 3-已结算"
    )
    pay_amount: Mapped[Decimal] = mapped_column(comment="实付金额")
    refund_amount: Mapped[Decimal | None] = mapped_column(comment="退款金额")
    refund_ratio: Mapped[Decimal | None] = mapped_column(comment="退款比例 (1.0=全额)")
    refund_reason: Mapped[str] = mapped_column(
        String(200), nullable=False, server_default=text("''"), comment="退款原因"
    )
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment="退款时间")
    joined_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=text("CURRENT_TIMESTAMP"), comment="加入时间"
    )

    def __repr__(self) -> str:
        return f"<Participant order={self.order_id} user={self.user_id} role={self.role}>"
