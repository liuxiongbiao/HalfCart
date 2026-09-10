"""
支付宝支付流水模型 — t_alipay_records
=====================================
PRD V3.2: 拼单订单支付宝支付, 完整流水记录, 双唯一索引幂等。

状态流转:
  PENDING(0) → SUCCESS(1) / CLOSED(2)
  SUCCESS(1) → FULL_REFUND(3) / PARTIAL_REFUND(4)
"""

from decimal import Decimal
from typing import Optional

from sqlalchemy import BigInteger, DateTime, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class AlipayTradeStatus:
    """支付宝支付单状态枚举。"""
    PENDING = 0          # 待支付
    SUCCESS = 1          # 支付成功
    CLOSED = 2           # 已关闭 (超时/取消)
    FULL_REFUND = 3      # 全额退款
    PARTIAL_REFUND = 4   # 部分退款

    @classmethod
    def label(cls, status: int) -> str:
        labels = {0: "待支付", 1: "支付成功", 2: "已关闭", 3: "全额退款", 4: "部分退款"}
        return labels.get(status, "未知")


class AlipayRecord(BaseModel):
    """支付宝支付流水表 — t_alipay_records。"""

    __tablename__ = "t_alipay_records"

    # ── 单号 (双重幂等) ──
    out_trade_no: Mapped[str] = mapped_column(
        String(32), nullable=False, unique=True, index=True,
        comment="商户订单号 HC+时间戳+随机串",
    )
    trade_no: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, unique=True, index=True,
        comment="支付宝交易号 (支付成功后回写)",
    )

    # ── 业务关联 ──
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="支付用户ID"
    )
    order_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="关联订单ID"
    )

    # ── 金额 ──
    amount: Mapped[Decimal] = mapped_column(
        comment="支付金额 (元, DECIMAL 12,2)"
    )
    refund_amount: Mapped[Optional[Decimal]] = mapped_column(
        nullable=True, comment="退款金额 (元)"
    )

    # ── 商品描述 ──
    subject: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="商品标题"
    )
    body: Mapped[str] = mapped_column(
        String(500), nullable=False, server_default=text("''"), comment="商品描述"
    )

    # ── 状态与时间 ──
    trade_status: Mapped[int] = mapped_column(
        default=AlipayTradeStatus.PENDING,
        server_default=text("0"),
        comment="0-待支付 1-成功 2-已关闭 3-全额退款 4-部分退款",
    )
    pay_time: Mapped[Optional[DateTime]] = mapped_column(
        DateTime, nullable=True, comment="支付宝支付时间"
    )
    notify_time: Mapped[Optional[DateTime]] = mapped_column(
        DateTime, nullable=True, comment="异步回调到达时间"
    )

    # ── 退款 ──
    refund_reason: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True, comment="退款原因"
    )

    # ── 排障 ──
    raw_notify: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="原始回调 JSON (对账排错用)"
    )

    def __repr__(self) -> str:
        return (
            f"<AlipayRecord id={self.id} out_trade_no={self.out_trade_no} "
            f"amount={self.amount} status={self.trade_status}>"
        )
