"""
运营后台 — 提现审核管理 (原 from api/v1/withdraw.py)
=====================================================
PRD 4.10: 提现审核 + 全量查询, 统一收归 /admin 前缀。
权限: 运营角色。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_required
from app.core.database import get_db
from app.schemas.common import APIResponse, PaginationResponse
from app.schemas.withdraw import AuditRequest
from app.services import withdraw_service
from app.utils.constants import WithdrawStatus

router = APIRouter()


def _mask_account(account: str) -> str:
    return account[:3] + "****" + account[-4:] if len(account) > 7 else "****"


def _record_dict(a) -> dict:
    return {
        "withdraw_id": a.id, "withdraw_no": a.withdraw_no,
        "amount": str(a.amount), "status": a.status,
        "status_label": WithdrawStatus(a.status).name,
        "pay_method": a.pay_method,
        "pay_method_label": "微信" if a.pay_method == 1 else "支付宝",
        "pay_account_masked": _mask_account(a.pay_account),
        "audit_remark": a.audit_remark,
        "applied_at": a.created_at.isoformat() if a.created_at else None,
        "audited_at": a.audited_at.isoformat() if a.audited_at else None,
    }


@router.post("/audit", summary="审核提现 (运营)")
async def audit(
    req: AuditRequest,
    db: AsyncSession = Depends(get_db),
    _user: int = Depends(admin_required),
):
    approved = req.audit_result == "approved"
    app = await withdraw_service.audit_withdraw(
        db, req.withdraw_id, approved=approved, remark=req.remark, auditor_id=_user,
    )
    msg = "审核通过, 已标记打款" if approved else "审核拒绝, 金额已退回用户钱包"
    return APIResponse.success(data=_record_dict(app), message=msg)


@router.get("", summary="全量提现记录 (运营)")
async def admin_list(
    status: Optional[int] = Query(default=None),
    user_id_filter: Optional[int] = Query(default=None, alias="filter_user_id"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user: int = Depends(admin_required),
):
    apps, total = await withdraw_service.list_all_withdraws(
        db, status=status, user_id=user_id_filter, page=page, page_size=page_size
    )
    return APIResponse.success(
        data=PaginationResponse(
            total=total, page=page, page_size=page_size,
            items=[_record_dict(a) for a in apps],
        ).model_dump()
    )
