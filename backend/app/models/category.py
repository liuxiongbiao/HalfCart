"""
品类字典 ORM 模型
===============
对应 categories 表，提供品类元数据管理。
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, comment="品类标识")
    label: Mapped[str] = mapped_column(String(30), nullable=False, comment="品类名称")
    icon: Mapped[str] = mapped_column(String(10), nullable=False, default="", server_default="", comment="图标emoji")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0", comment="排序权重")
    is_active: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1", comment="是否启用")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Category(id={self.id}, key='{self.key}', label='{self.label}')>"
