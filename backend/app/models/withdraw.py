"""
提现申请模型 — t_withdraw_applications
========================================
底座设计 2.7: 用户提现申请记录, MVP阶段线下打款模式。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class WithdrawApplication(BaseModel):
    """提现申请表 — t_withdraw_applications (底座设计 2.7)。"""

    __tablename__ = "t_withdraw_applications"

    withdraw_no: Mapped[str] = mapped_column(
        String(22), nullable=False, unique=True, comment="提现单号"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="用户ID"
    )
    amount: Mapped[Decimal] = mapped_column(comment="提现金额 ≥10 ≤5000")
    pay_method: Mapped[int] = mapped_column(comment="收款方式 1-微信 2-支付宝")
    pay_account: Mapped[str] = mapped_column(String(255), nullable=False, comment="收款账号 (加密存储)")
    real_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="收款人姓名 (加密存储)")
    status: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), index=True, comment="0-待审核 1-已打款 2-审核拒绝 3-打款失败"
    )
    audit_remark: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("''"), comment="审核备注"
    )
    audited_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="审核时间"
    )
