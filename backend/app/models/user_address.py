"""
用户收货地址模型 — t_user_addresses
=====================================
PRD 4.4: 交接地点管理, 支持默认地址。
"""

from sqlalchemy import BigInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class UserAddress(BaseModel):
    """用户收货地址表 — t_user_addresses。"""

    __tablename__ = "t_user_addresses"

    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="用户ID"
    )
    name: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="收货人姓名"
    )
    phone: Mapped[str] = mapped_column(
        String(256), nullable=False, comment="手机号 (AES-256-CBC加密)"
    )
    province: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="省份"
    )
    city: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="城市"
    )
    district: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="区县"
    )
    detail: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="详细地址"
    )
    is_default: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="是否默认 0-否 1-是"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "phone": self.phone,
            "province": self.province,
            "city": self.city,
            "district": self.district,
            "detail": self.detail,
            "full_address": f"{self.province}{self.city}{self.district} {self.detail}",
            "is_default": bool(self.is_default),
        }
