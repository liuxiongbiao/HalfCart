"""
用户收款账户模型 — t_user_payment_accounts
============================================
支持支付宝/微信收款账户 CRUD + 默认账户设置。
real_name / account 字段 AES-256-CBC 加密存储。
"""
from sqlalchemy import BigInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import decrypt_phone
from app.models.base import BaseModel


class UserPaymentAccount(BaseModel):
    """用户收款账户表 — t_user_payment_accounts。"""

    __tablename__ = "t_user_payment_accounts"

    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True, comment="用户ID"
    )
    account_type: Mapped[int] = mapped_column(
        comment="收款方式 1-微信 2-支付宝"
    )
    real_name: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="收款人姓名 (AES-256-CBC加密)"
    )
    account: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="收款账号 (AES-256-CBC加密)"
    )
    is_default: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="是否默认 0-否 1-是"
    )

    def to_dict(self) -> dict:
        # 解密敏感字段 (通过 decrypt_phone 复用 AES-256-CBC)
        real = _safe_decrypt(self.real_name)
        acct = _safe_decrypt(self.account)
        return {
            "id": self.id,
            "account_type": self.account_type,
            "account_type_label": "微信" if self.account_type == 1 else "支付宝",
            "real_name": real,
            "account": acct,
            "account_masked": _mask(acct),
            "is_default": bool(self.is_default),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def _safe_decrypt(value: str) -> str:
    """安全解密: 失败时返回原值 (兼容明文存量数据)。"""
    try:
        return decrypt_phone(value)
    except Exception:
        return value


def _mask(acct: str) -> str:
    """收款账号脱敏: 前三 + **** + 后四。"""
    if not acct or len(acct) <= 7:
        return acct or "****"
    return acct[:3] + "****" + acct[-4:]
