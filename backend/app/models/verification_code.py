"""
核销码模型 — t_verification_codes
=================================
底座设计 2.6: 订单与核销码一对一, 独立建表避免核销操作行锁影响订单主表并发查询。
PRD 4.5: 6位数字, 一次性有效, 错误≥5次锁定10分钟。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class VerificationCode(BaseModel):
    """核销码表 — t_verification_codes (底座设计 2.6)。"""

    __tablename__ = "t_verification_codes"

    order_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, comment="订单ID (一对一)"
    )
    code: Mapped[str] = mapped_column(
        String(6), nullable=False, index=True, comment="6位数字核销码"
    )
    is_used: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="0-未使用 1-已使用"
    )
    error_count: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="连续错误次数 ≥5锁定10min"
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="锁定截止时间"
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="核销时间"
    )
