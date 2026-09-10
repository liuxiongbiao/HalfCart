"""
短信发送日志模型 — t_sms_logs
==============================
记录每次短信发送的手机号、场景、模板、状态、耗时。
"""

from sqlalchemy import BigInteger, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import BaseModel


class SmsLog(BaseModel):
    """短信发送日志表。"""

    __tablename__ = "t_sms_logs"

    phone: Mapped[str] = mapped_column(String(128), nullable=False, comment="手机号 (加密存储)")
    scene: Mapped[str] = mapped_column(String(32), nullable=False, comment="场景: login/withdraw/notice")
    template_code: Mapped[str] = mapped_column(String(32), nullable=False, comment="阿里云模板CODE")
    status: Mapped[int] = mapped_column(default=1, server_default=text("1"), comment="1-成功 0-失败")
    error_msg: Mapped[str] = mapped_column(String(500), nullable=False, server_default=text("''"), comment="失败原因")
    duration_ms: Mapped[int] = mapped_column(default=0, comment="耗时ms")
    out_id: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"), comment="外部流水号")
