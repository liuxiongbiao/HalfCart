"""
提现管理 API — /api/v1/withdraw/*
==================================
PRD 4.10: 用户申请 / 记录查询 / 运营审核。
"""

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.schemas.common import APIResponse, PaginationResponse
from app.schemas.withdraw import AuditRequest, WithdrawApplyRequest
from app.services import withdraw_service
from app.utils.constants import PayMethod, WithdrawStatus

router = APIRouter()
from app.api.deps import get_current_user



def _mask_account(account: str) -> str:
    """收款账号脱敏: 前三 + **** + 后四。"""
    if len(account) > 7:
        return account[:3] + "****" + account[-4:]
    return "****"


def _record_dict(a) -> dict:
    return {
        "withdraw_id": a.id,
        "withdraw_no": a.withdraw_no,
        "amount": str(a.amount),
        "status": a.status,
        "status_label": WithdrawStatus(a.status).name if a.status in [0,1,2,3] else "未知",
        "pay_method": a.pay_method,
        "pay_method_label": "微信" if a.pay_method == 1 else "支付宝",
        "pay_account_masked": _mask_account(a.pay_account),
        "audit_remark": a.audit_remark,
        "applied_at": a.created_at.isoformat() if a.created_at else None,
        "audited_at": a.audited_at.isoformat() if a.audited_at else None,
    }


# ══════════════════════════════════════════════
# 用户端
# ══════════════════════════════════════════════


@router.post("/apply", summary="发起提现申请")
async def apply(
    req: WithdrawApplyRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """PRD 4.10: 提交提现申请 → 扣钱包余额 → 待审核。"""
    app = await withdraw_service.apply_withdraw(
        db, user_id,
        amount=Decimal(req.amount),
        payee_name=req.payee_name,
        payee_account=req.payee_account,
        pay_method=req.pay_method,
    )
    return APIResponse.success(
        data=_record_dict(app),
        message=f"提现申请已提交, 单号 {app.withdraw_no}, 每周二统一处理打款 (PRD 3.2)",
    )


@router.get("/list", summary="我的提现记录")
async def my_withdraws(
    status: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    apps, total = await withdraw_service.list_user_withdraws(
        db, user_id, status=status, page=page, page_size=page_size
    )
    return APIResponse.success(
        data=PaginationResponse(
            total=total, page=page, page_size=page_size,
            items=[_record_dict(a) for a in apps],
        ).model_dump()
    )


# 运营端接口已迁移至 /api/v1/admin/withdraws，详见 admin/withdraws.py
