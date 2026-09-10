"""
用户地址管理 API — /api/v1/user/addresses/*
==============================================
PRD 4.4: 收货地址 CRUD + 默认地址设置。
phone 字段 AES-256-CBC 加密存储。
"""

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.security import decrypt_phone, encrypt_phone
from app.models.user_address import UserAddress
from app.schemas.common import APIResponse, ErrorCode

router = APIRouter()


def _decrypt_address(addr: UserAddress) -> dict:
    """解密地址中的 phone 字段 (兼容明文存量数据)。"""
    phone = addr.phone
    try:
        phone = decrypt_phone(addr.phone)
    except Exception:
        pass  # 存量明文数据, 直接使用
    return {
        "id": addr.id,
        "name": addr.name,
        "phone": phone,
        "province": addr.province,
        "city": addr.city,
        "district": addr.district,
        "detail": addr.detail,
        "full_address": f"{addr.province}{addr.city}{addr.district} {addr.detail}",
        "is_default": bool(addr.is_default),
    }


class AddressRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=20, description="收货人")
    phone: str = Field(..., min_length=11, max_length=11, pattern=r"^1[3-9]\d{9}$")
    province: str = Field(..., min_length=1, max_length=20)
    city: str = Field(..., min_length=1, max_length=20)
    district: str = Field(..., min_length=1, max_length=20)
    detail: str = Field(..., min_length=1, max_length=100)
    is_default: bool = Field(default=False)


class AddressUpdateRequest(AddressRequest):
    pass


@router.get("/addresses", summary="地址列表")
async def list_addresses(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    result = await db.execute(
        select(UserAddress)
        .where(UserAddress.user_id == user_id)
        .order_by(UserAddress.is_default.desc(), UserAddress.updated_at.desc())
    )
    addresses = result.scalars().all()
    return APIResponse.success(
        data={"items": [_decrypt_address(a) for a in addresses], "total": len(addresses)}
    )


@router.post("/addresses", summary="新增地址")
async def create_address(
    req: AddressRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    if req.is_default:
        await db.execute(
            update(UserAddress).where(UserAddress.user_id == user_id).values(is_default=0)
        )
    data = req.model_dump()
    data["phone"] = encrypt_phone(data["phone"])  # 加密存储
    addr = UserAddress(user_id=user_id, **data)
    db.add(addr)
    await db.commit()
    await db.refresh(addr)
    return APIResponse.success(data=_decrypt_address(addr), message="地址已添加")


@router.put("/addresses/{address_id}", summary="编辑地址")
async def update_address(
    address_id: int,
    req: AddressUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    addr = await db.get(UserAddress, address_id)
    if not addr or addr.user_id != user_id:
        return APIResponse.error(code=ErrorCode.RESOURCE_NOT_FOUND, message="地址不存在")
    if req.is_default:
        await db.execute(
            update(UserAddress).where(UserAddress.user_id == user_id).values(is_default=0)
        )
    update_data = req.model_dump()
    update_data["phone"] = encrypt_phone(update_data["phone"])  # 加密存储
    for k, v in update_data.items():
        setattr(addr, k, v)
    db.add(addr)
    await db.commit()
    await db.refresh(addr)
    return APIResponse.success(data=_decrypt_address(addr), message="地址已更新")


@router.delete("/addresses/{address_id}", summary="删除地址")
async def delete_address(
    address_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    addr = await db.get(UserAddress, address_id)
    if not addr or addr.user_id != user_id:
        return APIResponse.error(code=ErrorCode.RESOURCE_NOT_FOUND, message="地址不存在")
    await db.delete(addr)
    await db.commit()
    return APIResponse.success(message="地址已删除")
