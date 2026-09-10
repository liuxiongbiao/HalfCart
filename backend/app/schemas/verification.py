"""
核销与退款 Schema
=================
PRD 4.5: 核销码验证 + 结算
PRD 4.6: 阶梯退款
"""

from pydantic import BaseModel, Field


class VerifyRequest(BaseModel):
    """核销请求 (PRD 4.5)。"""
    verify_code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$", description="6位数字核销码")


class RefundResponse(BaseModel):
    """退款操作结果 (PRD 4.6 阶梯退款)。"""
    order_id: int = Field(..., description="订单ID")
    status: int = Field(..., description="当前订单状态")
    refund_amount: str = Field(..., description="退款到账金额")
    penalty_amount: str = Field(default="0", description="违约金金额 (采购中为20%, 集资中为0)")
    refund_ratio: str = Field(..., description="退款比例 (1.0=全额)")
    wallet_after: str = Field(..., description="退款后钱包余额")


class SettlementResponse(BaseModel):
    """核销结算结果 (PRD 4.5)。"""
    order_id: int = Field(..., description="订单ID")
    status: int = Field(..., description="订单状态 (3-已完成)")
    total_amount: str = Field(..., description="订单总金额")
    service_fee: str = Field(..., description="平台服务费 (3%)")
    income: str = Field(..., description="团长实际收入 (97%)")
    wallet_after: str = Field(..., description="团长结算后钱包余额")
