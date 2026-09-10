"""
运营后台 — 用户管理
====================
PRD 3.1: 用户查询、封禁/解封、信用分调整。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_required
from app.core.database import get_db
from app.core.security import decrypt_phone, mask_phone
from app.models.user import User
from app.schemas.common import APIResponse, ErrorCode, PaginationResponse
from app.utils.constants import UserStatus


def _safe_mask_phone(phone: str) -> str:
    """先解密再脱敏。若解密失败则返回 '****'。"""
    try:
        return mask_phone(decrypt_phone(phone))
    except Exception:
        return "****"

router = APIRouter()


class AdjustCreditRequest(BaseModel):
    user_id: int = Field(..., gt=0)
    delta: int = Field(..., ge=-20, le=20, description="信用分变化量 正=加分 负=减分")


class BanRequest(BaseModel):
    user_id: int = Field(..., gt=0)
    action: str = Field(..., pattern="^(ban|unban)$")


@router.get("/users", summary="用户列表")
async def list_users(
    status: Optional[int] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _user: int = Depends(admin_required),
):
    stmt = select(User)
    cnt = select(func.count(User.id))
    if status is not None:
        stmt = stmt.where(User.status == status)
        cnt = cnt.where(User.status == status)
    if keyword:
        stmt = stmt.where(User.nickname.contains(keyword))
        cnt = cnt.where(User.nickname.contains(keyword))

    total = (await db.execute(cnt)).scalar() or 0
    stmt = stmt.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    users = (await db.execute(stmt)).scalars().all()

    items = [{
        "user_id": u.id, "nickname": u.nickname,
        "phone_masked": _safe_mask_phone(u.phone),
        "credit_score": u.credit_score,
        "wallet_balance": str(u.wallet_balance), "frozen_balance": str(u.frozen_balance),
        "status": u.status, "status_label": "正常" if u.status == 1 else "封禁",
        "created_at": u.created_at.isoformat() if u.created_at else None,
    } for u in users]
    return APIResponse.success(data=PaginationResponse(total=total, page=page, page_size=page_size, items=items).model_dump())


@router.post("/users/adjust-credit", summary="调整信用分")
async def adjust_credit(
    req: AdjustCreditRequest,
    db: AsyncSession = Depends(get_db),
    _admin: int = Depends(admin_required),
):
    u = await db.get(User, req.user_id)
    if not u:
        return APIResponse.error(code=ErrorCode.RESOURCE_NOT_FOUND.value, message="用户不存在")
    u.credit_score = max(0, min(120, u.credit_score + req.delta))
    db.add(u)
    await db.commit()
    return APIResponse.success(data={"user_id": u.id, "credit_score": u.credit_score}, message="信用分已调整")


@router.post("/users/ban", summary="封禁/解封用户")
async def ban_user(
    req: BanRequest,
    db: AsyncSession = Depends(get_db),
    _admin: int = Depends(admin_required),
):
    u = await db.get(User, req.user_id)
    if not u:
        return APIResponse.error(code=ErrorCode.RESOURCE_NOT_FOUND.value, message="用户不存在")
    u.status = UserStatus.BANNED if req.action == "ban" else UserStatus.ACTIVE
    db.add(u)
    await db.commit()
    return APIResponse.success(data={"user_id": u.id, "status": u.status}, message="已封禁" if req.action == "ban" else "已解封")
