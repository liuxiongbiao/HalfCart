"""
用户个人中心 Schema
===================
PRD 3.1/3.2: 个人信息查询/修改、首页聚合、资金流水。
"""

from typing import Optional
from pydantic import BaseModel, Field


class UserProfileResponse(BaseModel):
    """用户基础信息 (PRD 4.1/5.3: 手机号脱敏)。"""
    user_id: int = Field(...)
    phone_masked: str = Field(..., description="手机号脱敏 138****1234")
    nickname: str = Field(..., description="用户昵称")
    avatar_url: str = Field(default="", description="头像URL")
    credit_score: int = Field(..., description="信用分 (0-120)")
    wallet_balance: str = Field(..., description="可提现钱包余额")
    frozen_balance: str = Field(..., description="冻结担保金")
    created_at: Optional[str] = Field(default=None, description="注册时间")


class UpdateProfileRequest(BaseModel):
    """可修改的用户信息 (仅昵称/头像 — PRD 3.1)。"""
    nickname: Optional[str] = Field(default=None, max_length=30)
    avatar_url: Optional[str] = Field(default=None, max_length=255)


class HomePageResponse(BaseModel):
    """个人中心首页聚合 (PRD 3.1)。"""
    profile: UserProfileResponse
    active_created_count: int = Field(default=0, description="我发起的进行中订单数 (status<3)")
    active_joined_count: int = Field(default=0, description="我参与的进行中订单数 (status<3)")
    completed_count: int = Field(default=0, description="已完成订单总数")


class PaymentLogItem(BaseModel):
    """资金流水项 (底座 2.5)。"""
    log_no: str = Field(...)
    log_type: int = Field(...)
    log_type_label: str = Field(default="")
    amount: str = Field(...)
    balance_before: str = Field(default="0")
    balance_after: str = Field(default="0")
    order_id: Optional[int] = Field(default=None)
    remark: str = Field(default="")
    created_at: Optional[str] = Field(default=None)
