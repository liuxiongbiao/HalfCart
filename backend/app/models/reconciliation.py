"""
对账结果记录模型
================
PRD 4.5 分布式事务补偿: 对账结果持久化, 异常明细可追溯。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class ReconciliationRecord(BaseModel):
    """对账执行记录。"""
    __tablename__ = "t_reconciliation_records"

    recon_type: Mapped[str] = mapped_column(String(16), comment="incremental / full")
    cycle_start: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="对账周期开始")
    cycle_end: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="对账周期结束")
    orders_checked: Mapped[int] = mapped_column(default=0)
    users_checked: Mapped[int] = mapped_column(default=0)
    anomalies_total: Mapped[int] = mapped_column(default=0)
    auto_fixed: Mapped[int] = mapped_column(default=0)
    pending_manual: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="completed", server_default=text("'completed'"))
    duration_ms: Mapped[int] = mapped_column(default=0, comment="执行耗时ms")
    idempotent_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True, comment="幂等键: recon:{type}:{start}:{end}"
    )


class ReconciliationAnomaly(BaseModel):
    """对账异常明细。"""
    __tablename__ = "t_reconciliation_anomalies"

    record_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True, comment="对账记录ID")
    anomaly_type: Mapped[str] = mapped_column(String(32), comment="balance_mismatch / payment_gap / status_conflict")
    order_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    description: Mapped[str] = mapped_column(String(1000), nullable=False, server_default=text("''"))
    fix_status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default=text("'pending'"),
        comment="pending / auto_fixed / manual_required / resolved"
    )
    fix_remark: Mapped[str] = mapped_column(String(500), nullable=False, server_default=text("''"))
    expected_value: Mapped[str] = mapped_column(String(200), nullable=False, server_default=text("''"))
    actual_value: Mapped[str] = mapped_column(String(200), nullable=False, server_default=text("''"))
