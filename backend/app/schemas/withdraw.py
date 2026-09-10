"""
提现申请 Schema
===============
PRD 4.10: 提现申请/查询/审核。
"""

from typing import Optional
from pydantic import BaseModel, Field


class WithdrawApplyRequest(BaseModel):
    """提现申请请求 (PRD 4.10)。"""
    amount: str = Field(..., description="提现金额 (Decimal字符串, ≥10)")
    payee_name: str = Field(..., min_length=1, max_length=20, description="收款人姓名")
    payee_account: str = Field(..., min_length=1, max_length=50, description="收款账号")
    pay_method: int = Field(..., ge=1, le=2, description="收款方式: 1-微信 2-支付宝")


class AuditRequest(BaseModel):
    """运营审核请求。"""
    withdraw_id: int = Field(..., gt=0, description="提现记录ID")
    audit_result: str = Field(..., pattern="^(approved|rejected)$", description="审核结果: approved/rejected")
    remark: str = Field(default="", max_length=255, description="审核备注")


class WithdrawRecordItem(BaseModel):
    """提现记录响应 (收款账号脱敏)。"""
    withdraw_id: int = Field(...)
    withdraw_no: str = Field(...)
    amount: str = Field(...)
    status: int = Field(...)
    status_label: str = Field(default="")
    pay_method: int = Field(...)
    pay_method_label: str = Field(default="")
    pay_account_masked: str = Field(default="", description="收款账号脱敏")
    audit_remark: str = Field(default="")
    applied_at: Optional[str] = Field(default=None)
    audited_at: Optional[str] = Field(default=None)
