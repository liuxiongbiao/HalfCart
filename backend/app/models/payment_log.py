"""
资金流水数据模型 — t_payment_logs
================================
底座设计 2.5: 每次资金变动同步写入流水，与余额变更在同一事务内完成。
PRD 5.3: 幂等键 (idempotent_key) UNIQUE 索引防止重复记账。
"""

from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class PaymentLog(BaseModel):
    """资金流水表 — t_payment_logs (底座设计 2.5)。"""

    __tablename__ = "t_payment_logs"

    # ── 流水标识 ──
    log_no: Mapped[str] = mapped_column(
        String(26), nullable=False, unique=True, index=True, comment="流水单号 全局唯一"
    )

    # ── 关联信息 ──
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="用户ID"
    )
    order_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, comment="关联订单ID (提现流水不关联订单)"
    )

    # ── 流水类型 ──
    type: Mapped[int] = mapped_column(comment="1-冻结 2-解冻 3-结算 4-违约金 5-服务费 6-提现 7-打款 8-退回 9-纠纷")

    # ── 金额 ──
    amount: Mapped[Decimal] = mapped_column(comment="变动金额 正=入账 负=出账")
    balance_before: Mapped[Decimal] = mapped_column(comment="变动前余额")
    balance_after: Mapped[Decimal] = mapped_column(comment="变动后余额")

    # ── 目标账户类型 ──
    target_balance_type: Mapped[int] = mapped_column(comment="1-wallet_balance 2-frozen_balance")

    # ── 状态 ──
    status: Mapped[int] = mapped_column(
        default=1, server_default=text("1"), comment="1-成功 0-失败"
    )

    # ── 备注 ──
    remark: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("''"), comment="备注"
    )

    # ── 幂等键 ──
    idempotent_key: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        unique=True,
        index=True,
        comment="幂等键 (SHA256+后缀), 最大80字符 — PRD 5.3 幂等防护核心",
    )

    def __repr__(self) -> str:
        return (
            f"<PaymentLog log_no={self.log_no} "
            f"user={self.user_id} type={self.type} amount={self.amount}>"
        )
