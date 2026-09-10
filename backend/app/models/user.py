"""
用户数据模型 — t_users
======================
对应 PRD 3.2 虚拟账本资产体系、4.1 用户鉴权
底座设计 2.2: wallet_balance + frozen_balance 双资金字段，DECIMAL(12,2) 明文存储

PRD 5.3 产品决策：
  放弃资金字段加密存储，通过数据库访问白名单 + 应用层参数验签 + 全量流水对账
  三重机制保障资金数据防篡改与可追溯，兼顾高并发原子运算性能。
"""

from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel
from app.utils.constants import CreditChange, UserStatus


class User(BaseModel):
    """用户表 — t_users (底座设计 2.2)。"""

    __tablename__ = "t_users"

    # ── 手机号 (AES加密存储 + SHA256哈希索引) ──
    phone: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, comment="手机号 AES-256-CBC 加密存储"
    )
    phone_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        comment="手机号 SHA256 哈希，快速查找索引",
    )

    # ── 账号密码登录 (PRD 4.1 扩展) ──
    username: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        unique=True,
        index=True,
        comment="用户账号 (字母+数字, 4-32位)",
    )
    password_hash: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="密码哈希值 HMAC-SHA256",
    )

    # ── 微信绑定 (PRD 4.1 扩展) ──
    wechat_openid: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        unique=True,
        index=True,
        comment="微信 OpenID，扫码登录绑定",
    )

    # ── 基本信息 ──
    nickname: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("''"), comment="用户昵称"
    )
    avatar_url: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("''"), comment="头像URL"
    )

    # ── 信用分 ──
    credit_score: Mapped[int] = mapped_column(
        default=CreditChange.INITIAL.value,
        server_default=text(str(CreditChange.INITIAL.value)),
        comment=f"信用分 0-120，初始{CreditChange.INITIAL.value}分",
    )

    # ── 双资金字段 (明文) ──
    wallet_balance: Mapped[Decimal] = mapped_column(
        default=Decimal("0.00"),
        server_default=text("0.00"),
        comment="可提现钱包余额 — 核销后扣除服务费的资金 (PRD 3.2)",
    )
    frozen_balance: Mapped[Decimal] = mapped_column(
        default=Decimal("0.00"),
        server_default=text("0.00"),
        comment="冻结担保金 — 拼友支付后平台托管 (PRD 3.2)",
    )

    # ── 账号状态 ──
    role: Mapped[int] = mapped_column(
        default=0, server_default=text("0"), comment="0-普通用户 1-管理员"
    )
    status: Mapped[int] = mapped_column(
        default=UserStatus.ACTIVE.value,
        server_default=text(str(UserStatus.ACTIVE.value)),
        comment="1-正常 0-封禁",
    )

    last_login_at: Mapped[DateTime | None] = mapped_column(
        DateTime, nullable=True, comment="最后登录时间"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} wallet={self.wallet_balance} frozen={self.frozen_balance}>"
