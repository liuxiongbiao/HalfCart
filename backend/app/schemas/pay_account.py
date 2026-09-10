"""
收款账户 Pydantic Schema — 请求/响应模型
==========================================
"""
from pydantic import BaseModel, Field

from app.utils.constants import PayMethod


class PayAccountCreate(BaseModel):
    """新增收款账户请求。"""
    account_type: int = Field(
        ..., ge=1, le=2, description="收款方式: 1-微信, 2-支付宝"
    )
    real_name: str = Field(
        ..., min_length=1, max_length=100, description="收款人姓名"
    )
    account: str = Field(
        ..., min_length=1, max_length=255, description="收款账号 (手机号/邮箱)"
    )


class PayAccountUpdate(BaseModel):
    """编辑收款账户请求。"""
    account_type: int = Field(
        ..., ge=1, le=2, description="收款方式: 1-微信, 2-支付宝"
    )
    real_name: str = Field(
        ..., min_length=1, max_length=100, description="收款人姓名"
    )
    account: str = Field(
        ..., min_length=1, max_length=255, description="收款账号 (手机号/邮箱)"
    )


class PayAccountResponse(BaseModel):
    """收款账户响应。"""
    id: int
    account_type: int
    account_type_label: str
    real_name: str
    account: str
    account_masked: str
    is_default: bool
    created_at: str | None = None
    updated_at: str | None = None
