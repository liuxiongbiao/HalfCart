"""
收款账户管理 API — /api/v1/user/payment-accounts/*
=====================================================
支持支付宝/微信收款账户 CRUD + 默认账户设置。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.schemas.common import APIResponse, ErrorCode
from app.schemas.pay_account import PayAccountCreate, PayAccountUpdate
from app.services import pay_account_service

router = APIRouter()


@router.get("/payment-accounts", summary="收款账户列表")
async def list_payment_accounts(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """获取当前用户的所有收款账户, 默认账户排在前面。"""
    accounts = await pay_account_service.list_accounts(db, user_id)
    return APIResponse.success(
        data={"items": [a.to_dict() for a in accounts], "total": len(accounts)}
    )


@router.post("/payment-accounts", summary="新增收款账户")
async def create_payment_account(
    req: PayAccountCreate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """新增收款账户 (每用户最多5个, 同类型只能一个)。"""
    acct, err = await pay_account_service.create_account(
        db, user_id,
        account_type=req.account_type,
        real_name=req.real_name,
        account=req.account,
    )
    if err:
        return APIResponse.error(code=ErrorCode.PARAM_VALIDATION_ERROR, message=err)
    return APIResponse.success(data=acct.to_dict(), message="收款账户已添加")


@router.put("/payment-accounts/{account_id}", summary="编辑收款账户")
async def update_payment_account(
    account_id: int,
    req: PayAccountUpdate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """编辑收款账户 (仅所有者可操作)。"""
    acct, err = await pay_account_service.update_account(
        db, user_id, account_id,
        account_type=req.account_type,
        real_name=req.real_name,
        account=req.account,
    )
    if err:
        code = ErrorCode.RESOURCE_NOT_FOUND if "不存在" in err else ErrorCode.PARAM_VALIDATION_ERROR
        return APIResponse.error(code=code, message=err)
    return APIResponse.success(data=acct.to_dict(), message="收款账户已更新")


@router.delete("/payment-accounts/{account_id}", summary="删除收款账户")
async def delete_payment_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """删除收款账户 (仅所有者可操作, 删除默认账户后自动切换)。"""
    err = await pay_account_service.delete_account(db, user_id, account_id)
    if err:
        return APIResponse.error(code=ErrorCode.RESOURCE_NOT_FOUND, message=err)
    return APIResponse.success(message="收款账户已删除")


@router.put("/payment-accounts/{account_id}/default", summary="设为默认收款账户")
async def set_default_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """设为默认收款账户 (仅所有者可操作)。"""
    err = await pay_account_service.set_default(db, user_id, account_id)
    if err:
        return APIResponse.error(code=ErrorCode.RESOURCE_NOT_FOUND, message=err)
    return APIResponse.success(message="已设为默认收款账户")
