"""
资金服务测试接口 — /api/v1/test/balance/*
==========================================
无需鉴权, 直接暴露 balance_service 6 个原子操作, 方便开发调试。

接口列表:
  POST /create-user          — 创建测试用户
  POST /freeze-increase       — 冻结余额增加 (拼友支付)
  POST /freeze-decrease       — 冻结余额扣减 (退款)
  POST /wallet-increase       — 钱包余额增加 (核销结算/违约金)
  POST /wallet-decrease       — 钱包余额扣减 (提现)
  POST /settlement            — 冻结转钱包 (核销结算, 扣3%服务费)
  POST /penalty-transfer      — 资金划转 (违约金)
  GET  /user/{user_id}        — 查看用户余额
"""

from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.user import User
from app.schemas.common import APIResponse
from app.services.balance_service import (
    freeze_balance_decrease,
    freeze_balance_increase,
    frozen_to_wallet_settlement,
    transfer_penalty,
    wallet_balance_decrease,
    wallet_balance_increase,
)
from app.utils.constants import PaymentLogType
from app.utils.idempotent import generate_idempotent_key

router = APIRouter()

# ── 全局守卫：所有测试接口在 TEST_MODE=false 时禁用 ──
from fastapi import Depends as _Depends, HTTPException as _HTTPException
from app.config import settings


async def _require_test_mode():
    """依赖注入守卫：TEST_MODE=false 时拒绝所有请求。"""
    if not settings.TEST_MODE:
        raise _HTTPException(
            status_code=403,
            detail="TEST_MODE 已关闭，测试接口不可用",
        )
    if settings.APP_ENV == "production":
        raise _HTTPException(
            status_code=403,
            detail="生产环境禁止调用测试接口",
        )


# ══════════════════════════════════════════════
# 请求 Schema
# ══════════════════════════════════════════════


class CreateTestUserRequest(BaseModel):
    """创建测试用户 — 不需要参数, 自动生成唯一标识。"""


class BalanceOperationRequest(BaseModel):
    """通用资金操作请求 — 暴露 user_id / amount / idempotent_key。"""

    user_id: int = Field(..., gt=0, description="用户ID")
    amount: str = Field(..., description="金额 (Decimal字符串, 如 '100.50')")
    idempotent_key: str = Field(..., min_length=8, description="幂等键 (唯一业务流水号)")


class SettlementRequest(BaseModel):
    """核销结算请求。"""

    user_id: int = Field(..., gt=0, description="团长用户ID")
    total_amount: str = Field(..., description="订单总金额 (Decimal字符串)")
    idempotent_key: str = Field(..., min_length=8, description="幂等键")


class PenaltyTransferRequest(BaseModel):
    """违约金划转请求。"""

    from_user_id: int = Field(..., gt=0, description="出账方ID (拼友)")
    to_user_id: int = Field(..., gt=0, description="入账方ID (团长)")
    amount: str = Field(..., description="划转金额")
    idempotent_key: str = Field(..., min_length=8, description="幂等键")


class FreezeIncreaseRequest(BaseModel):
    """冻结余额增加 (带 order_id)。"""

    user_id: int = Field(..., gt=0, description="团长用户ID")
    order_id: int = Field(..., gt=0, description="订单ID")
    amount: str = Field(..., description="冻结金额")
    idempotent_key: str = Field(..., min_length=8, description="幂等键")


class FreezeDecreaseRequest(BaseModel):
    """冻结余额扣减 (带 order_id)。"""

    user_id: int = Field(..., gt=0, description="用户ID")
    order_id: int = Field(..., gt=0, description="订单ID")
    amount: str = Field(..., description="扣减金额")
    idempotent_key: str = Field(..., min_length=8, description="幂等键")


class WalletIncreaseRequest(BaseModel):
    """钱包余额增加。"""

    user_id: int = Field(..., gt=0, description="用户ID")
    order_id: int = Field(..., gt=0, description="订单ID")
    amount: str = Field(..., description="增加金额")
    idempotent_key: str = Field(..., min_length=8, description="幂等键")


# ══════════════════════════════════════════════
# 响应辅助
# ══════════════════════════════════════════════


async def _get_user_balance(db: AsyncSession, user_id: int) -> dict:
    """查询用户当前余额。"""
    from sqlalchemy import select

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        return {}
    return {
        "user_id": user.id,
        "wallet_balance": str(user.wallet_balance),
        "frozen_balance": str(user.frozen_balance),
        "credit_score": user.credit_score,
    }


def _to_decimal(amount_str: str) -> Decimal:
    """安全转换金额字符串为 Decimal。"""
    return Decimal(amount_str).quantize(Decimal("0.01"))


# 将 TEST_MODE 守卫注入到路由的所有端点
router.dependencies.append(_Depends(_require_test_mode))

# ══════════════════════════════════════════════
# 测试接口
# ══════════════════════════════════════════════


@router.post("/create-user", summary="创建测试用户")
async def create_test_user(
    req: CreateTestUserRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    创建一个初始余额为 0 的测试用户。

    自动生成唯一的手机号和哈希, 信用分初始 100。
    """
    import uuid

    uid = str(uuid.uuid4().hex)[:16]
    user = User(
        phone=f"test_encrypted_{uid}",
        phone_hash=f"test_hash_{uid}",
        nickname=f"TestUser_{uid[:6]}",
        wallet_balance=Decimal("0.00"),
        frozen_balance=Decimal("0.00"),
        credit_score=100,
        status=1,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return APIResponse.success(
        data={
            "user_id": user.id,
            "nickname": user.nickname,
            "wallet_balance": str(user.wallet_balance),
            "frozen_balance": str(user.frozen_balance),
            "credit_score": user.credit_score,
        },
        message=f"测试用户创建成功 (ID={user.id})",
    )


@router.post("/freeze-increase", summary="冻结余额增加 (拼友支付)")
async def test_freeze_increase(
    req: FreezeIncreaseRequest,
    db: AsyncSession = Depends(get_db),
):
    """拼友支付 → 团长 frozen_balance += amount。"""
    amount = _to_decimal(req.amount)
    log = await freeze_balance_increase(
        session=db,
        user_id=req.user_id,
        order_id=req.order_id,
        amount=amount,
        idempotent_key=req.idempotent_key,
        remark=f"测试: 订单 {req.order_id} 拼友支付冻结",
    )
    balance = await _get_user_balance(db, req.user_id)
    return APIResponse.success(
        data={
            "log_no": log.log_no,
            "type": PaymentLogType.label(log.type),
            "amount": str(log.amount),
            "balance": balance,
        },
        message="冻结余额增加成功",
    )


@router.post("/freeze-decrease", summary="冻结余额扣减 (退款)")
async def test_freeze_decrease(
    req: FreezeDecreaseRequest,
    db: AsyncSession = Depends(get_db),
):
    """退款 → frozen_balance -= amount。"""
    amount = _to_decimal(req.amount)
    log = await freeze_balance_decrease(
        session=db,
        user_id=req.user_id,
        order_id=req.order_id,
        amount=amount,
        idempotent_key=req.idempotent_key,
        remark=f"测试: 订单 {req.order_id} 退款解冻",
    )
    balance = await _get_user_balance(db, req.user_id)
    return APIResponse.success(
        data={
            "log_no": log.log_no,
            "type": PaymentLogType.label(log.type),
            "amount": str(log.amount),
            "balance": balance,
        },
        message="冻结余额扣减成功",
    )


@router.post("/wallet-increase", summary="钱包余额增加 (结算/违约金)")
async def test_wallet_increase(
    req: WalletIncreaseRequest,
    db: AsyncSession = Depends(get_db),
):
    """核销/违约金 → wallet_balance += amount。"""
    amount = _to_decimal(req.amount)
    log = await wallet_balance_increase(
        session=db,
        user_id=req.user_id,
        order_id=req.order_id,
        amount=amount,
        log_type=PaymentLogType.SETTLEMENT,
        idempotent_key=req.idempotent_key,
        remark=f"测试: 订单 {req.order_id} 结算收入",
    )
    balance = await _get_user_balance(db, req.user_id)
    return APIResponse.success(
        data={
            "log_no": log.log_no,
            "type": PaymentLogType.label(log.type),
            "amount": str(log.amount),
            "balance": balance,
        },
        message="钱包余额增加成功",
    )


@router.post("/wallet-decrease", summary="钱包余额扣减 (提现)")
async def test_wallet_decrease(
    req: BalanceOperationRequest,
    db: AsyncSession = Depends(get_db),
):
    """提现 → wallet_balance -= amount (≥10元)。"""
    amount = _to_decimal(req.amount)
    log = await wallet_balance_decrease(
        session=db,
        user_id=req.user_id,
        amount=amount,
        idempotent_key=req.idempotent_key,
        remark="测试: 提现申请",
    )
    balance = await _get_user_balance(db, req.user_id)
    return APIResponse.success(
        data={
            "log_no": log.log_no,
            "type": PaymentLogType.label(log.type),
            "amount": str(log.amount),
            "balance": balance,
        },
        message="钱包余额扣减成功",
    )


@router.post("/settlement", summary="冻结转钱包 — 核销结算 (扣3%服务费)")
async def test_settlement(
    req: SettlementRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    核销结算:
      团长 frozen_balance -= total_amount
      团长 wallet_balance += (total_amount * 97%)
      平台抽 3% 服务费
    """
    amount = _to_decimal(req.total_amount)
    settlement_log, fee_log = await frozen_to_wallet_settlement(
        session=db,
        user_id=req.user_id,
        order_id=0,  # 测试
        total_amount=amount,
        idempotent_key=req.idempotent_key,
    )
    balance = await _get_user_balance(db, req.user_id)
    return APIResponse.success(
        data={
            "settlement_log": {
                "log_no": settlement_log.log_no,
                "amount": str(settlement_log.amount),
            },
            "fee_log": {
                "log_no": fee_log.log_no,
                "amount": str(fee_log.amount),
                "remark": fee_log.remark,
            },
            "balance": balance,
        },
        message=f"核销结算成功: 总金额 {amount}, 服务费 {fee_log.amount}",
    )


@router.post("/penalty-transfer", summary="资金划转 — 违约金 (frozen→wallet)")
async def test_penalty_transfer(
    req: PenaltyTransferRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    违约金划转:
      from_user.frozen_balance -= amount  (拼友)
      to_user.wallet_balance += amount    (团长)
    """
    amount = _to_decimal(req.amount)
    debit_log, credit_log = await transfer_penalty(
        session=db,
        from_user_id=req.from_user_id,
        to_user_id=req.to_user_id,
        order_id=0,  # 测试
        amount=amount,
        idempotent_key=req.idempotent_key,
    )
    from_balance = await _get_user_balance(db, req.from_user_id)
    to_balance = await _get_user_balance(db, req.to_user_id)
    return APIResponse.success(
        data={
            "debit": {
                "user_id": req.from_user_id,
                "log_no": debit_log.log_no,
                "amount": str(debit_log.amount),
                "balance": from_balance,
            },
            "credit": {
                "user_id": req.to_user_id,
                "log_no": credit_log.log_no,
                "amount": str(credit_log.amount),
                "balance": to_balance,
            },
        },
        message=f"违约金划转成功: {amount} 元",
    )


@router.get("/user/{user_id}", summary="查看用户余额")
async def get_user_balance(
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """查询用户当前钱包余额 & 冻结余额。"""
    balance = await _get_user_balance(db, user_id)
    if not balance:
        return APIResponse.error(code=1, message=f"用户不存在: {user_id}")
    return APIResponse.success(data=balance)
