"""
订单数据模型 — t_orders
=======================
底座设计 2.3: 订单表, 4态状态机, 支持普通拼单与现货转让。
PRD 4.2: GATHERING→PURCHASING→DELIVERING→FINISHED 单向流转。
"""

from decimal import Decimal
from datetime import datetime

from sqlalchemy import BigInteger, DECIMAL, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel
from app.utils.constants import OrderStatus, OrderType


class Order(BaseModel):
    """订单表 — t_orders (底座设计 2.3)。"""

    __tablename__ = "t_orders"

    # ── 单号 ──
    order_no: Mapped[str] = mapped_column(
        String(22), nullable=False, unique=True, index=True, comment="业务单号 HC+时间戳+随机串"
    )

    # ── 团长 ──
    creator_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="团长用户ID"
    )

    # ── 状态 ──
    status: Mapped[int] = mapped_column(
        default=OrderStatus.GATHERING,
        server_default=text(str(OrderStatus.GATHERING)),
        index=True,
        comment="0-集资中 1-采购中 2-待交接 3-已完成 4-已解散",
    )

    # ── 类型 ──
    order_type: Mapped[int] = mapped_column(
        default=OrderType.NORMAL,
        server_default=text(str(OrderType.NORMAL)),
        comment="1-普通拼单 2-现货转让",
    )

    # ── 商品信息 ──
    goods_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="商品名称"
    )
    goods_desc: Mapped[str] = mapped_column(
        String(500), nullable=False, server_default=text("''"), comment="商品描述"
    )
    goods_category: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("''"), index=True, comment="商品品类"
    )
    purchase_channel: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("''"), comment="采购渠道"
    )

    # ── 人数与金额 ──
    total_people: Mapped[int] = mapped_column(comment="需要总人数, 含团长 ≥2")
    current_people: Mapped[int] = mapped_column(default=1, server_default=text("1"), comment="已加入人数")
    price_per_person: Mapped[Decimal] = mapped_column(comment="人均价格")
    total_amount: Mapped[Decimal] = mapped_column(comment="订单总金额")

    # ── 地点 ──
    meet_location: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="交接地点"
    )
    location_lat: Mapped[Decimal] = mapped_column(DECIMAL(10, 7), comment="纬度")
    location_lng: Mapped[Decimal] = mapped_column(DECIMAL(10, 7), comment="经度")

    # ── 截单时间 ──
    deadline: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, index=True, comment="截单时间"
    )

    # ── 现货转让字段 ──
    receipt_img: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("''"), comment="小票原图URL"
    )
    original_price: Mapped[Decimal | None] = mapped_column(comment="小票原价, 现货转让使用")
    is_spot: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="现货标记 0-否 1-是"
    )
    spot_valid_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="现货有效期 (默认24h)"
    )

    # ── 纠纷 ──
    dispute_status: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="0-无 1-处理中 2-已结案"
    )

    # ── 完成时间 ──
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="核销完成时间"
    )

    # ── 软删除 ──
    is_deleted: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="软删除 0-否 1-是"
    )

    def __repr__(self) -> str:
        return f"<Order id={self.id} no={self.order_no} status={self.status}>"
