"""
ORM 基类 (SQLAlchemy 2.0 DeclarativeBase)
==========================================
所有数据模型统一继承此类，提供 id, created_at, updated_at 通用字段。
底座设计 2.2-2.8: 所有核心表均包含这些字段。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class TimestampMixin:
    """时间戳混入类：自动管理 created_at / updated_at。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.now,
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class BaseModel(Base, TimestampMixin):
    """全局 ORM 基类。提供自增主键 id。"""

    __abstract__ = True

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
