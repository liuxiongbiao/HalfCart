"""
拼单参与 Schema — 加入/退出请求与响应
=======================================
PRD 4.4/4.6: 拼友视角的加入与退出操作。
"""

from pydantic import BaseModel, Field


class JoinOrderResponse(BaseModel):
    """加入拼单操作结果 (PRD 4.4 拼友视角)。"""

    order_id: int = Field(..., description="订单ID")
    status: int = Field(..., description="当前订单状态")
    status_label: str = Field(..., description="状态中文标签")
    current_people: int = Field(..., description="当前参与人数")
    total_people: int = Field(..., description="总人数")
    remaining_slots: int = Field(..., description="剩余名额")
    deducted_amount: str = Field(..., description="从钱包扣除的金额")
    wallet_balance_after: str = Field(..., description="操作后钱包余额")


class QuitOrderResponse(BaseModel):
    """退出拼单操作结果 (PRD 4.6 全额退款)。"""

    order_id: int = Field(..., description="订单ID")
    refunded_amount: str = Field(..., description="退回金额 (全额)")
    wallet_balance_after: str = Field(..., description="操作后钱包余额")
    current_people: int = Field(..., description="退出后参与人数")
    credit_score_change: int = Field(default=-1, description="信用分变动 (GATHERING退出-1)")
