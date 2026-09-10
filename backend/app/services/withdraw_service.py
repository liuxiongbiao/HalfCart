"""
提现申请与管理服务
==================
PRD 3.2/4.10: MVP阶段线下打款 — 申请扣减→人工审核→打款/退回。

约束:
  - 资金操作 → balance_service
  - 用户级分布式锁 → user:withdraw:{user_id}
  - 幂等: 同一流水号不重复扣钱
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.redis_client import RedisLock
from app.middleware.exception_handler import AppException
from app.models.user import User
from app.models.withdraw import WithdrawApplication
from app.schemas.common import ErrorCode
from app.services import balance_service
from app.utils.constants import PaymentLogType, WithdrawStatus
from app.utils.idempotent import generate_idempotent_key
from app.utils.privacy import mask_phone

logger = logging.getLogger(__name__)

MAX_SINGLE_WITHDRAW = Decimal("5000")   # 单笔最高
MAX_DAILY_COUNT = 3                     # 单日最多


def _gen_withdraw_no() -> str:
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    return f"HCW{ts}{uuid.uuid4().hex[:6]}"


# ══════════════════════════════════════════════
# 用户提现申请
# ══════════════════════════════════════════════


async def apply_withdraw(
    session: AsyncSession,
    user_id: int,
    amount: Decimal,
    payee_name: str,
    payee_account: str,
    pay_method: int,
) -> WithdrawApplication:
    """
    发起提现申请 (PRD 4.10)。

    流程: 校验→锁→扣钱包→写记录→提交
    """
    min_amt = Decimal(str(settings.WITHDRAW_MIN_AMOUNT))
    if amount < min_amt:
        raise AppException(code=ErrorCode.WITHDRAW_BELOW_MINIMUM, message=f"最低提现金额 {min_amt} 元")
    if amount > MAX_SINGLE_WITHDRAW:
        raise AppException(code=ErrorCode.WITHDRAW_EXCEED_LIMIT, message=f"单笔最高 {MAX_SINGLE_WITHDRAW} 元")

    # 校验: 同一用户仅允许一笔处理中的提现 (PRD 4.10)
    pending_result = await session.execute(
        select(func.count(WithdrawApplication.id)).where(
            WithdrawApplication.user_id == user_id,
            WithdrawApplication.status == 0,  # PENDING
        )
    )
    if (pending_result.scalar() or 0) > 0:
        raise AppException(
            code=ErrorCode.WITHDRAW_DUPLICATE_REQUEST,
            message="您有一笔提现正在处理中, 完成后可再次申请",
        )

    # 校验当日次数
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    count_result = await session.execute(
        select(func.count(WithdrawApplication.id)).where(
            WithdrawApplication.user_id == user_id,
            WithdrawApplication.created_at >= today,
        )
    )
    if (count_result.scalar() or 0) >= MAX_DAILY_COUNT:
        raise AppException(code=ErrorCode.WITHDRAW_DAILY_LIMIT, message=f"单日最多提现 {MAX_DAILY_COUNT} 次")

    # 校验余额
    user = await session.get(User, user_id)
    if not user or user.wallet_balance < amount:
        raise AppException(code=ErrorCode.WITHDRAW_EXCEED_BALANCE, message="可提现余额不足")

    lock = RedisLock(f"user:withdraw:{user_id}", timeout=15)
    async with lock as acquired:
        if not acquired:
            raise AppException(code=ErrorCode.RATE_LIMITED, message="提现操作繁忙, 请稍后重试")

        # 幂等键
        ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        idempotent_key = generate_idempotent_key("withdraw.apply", user_id, None, ts)

        # 扣钱包
        log = await balance_service.wallet_balance_decrease(
            session=session,
            user_id=user_id,
            amount=amount,
            idempotent_key=idempotent_key,
            remark=f"提现申请 {amount} 元",
        )

        # 创建提现记录
        app = WithdrawApplication(
            withdraw_no=_gen_withdraw_no(),
            user_id=user_id,
            amount=amount,
            pay_method=pay_method,
            pay_account=payee_account,
            real_name=payee_name,
            status=WithdrawStatus.PENDING,
        )
        session.add(app)
        await session.commit()
        await session.refresh(app)

        logger.info(f"提现申请: user={user_id} amount={amount} withdraw_no={app.withdraw_no}")
        return app


# ══════════════════════════════════════════════
# 运营审核
# ══════════════════════════════════════════════


async def audit_withdraw(
    session: AsyncSession,
    withdraw_id: int,
    approved: bool,
    remark: str = "",
    auditor_id: int = 0,
) -> WithdrawApplication:
    """
    审核提现申请 (PRD 4.10)。

    approved=True → 状态→已打款 (资金已在申请时扣减)
    approved=False → 状态→审核拒绝 → 退回余额
    """
    app = await session.get(WithdrawApplication, withdraw_id)
    if not app:
        raise AppException(code=ErrorCode.WITHDRAW_NOT_FOUND, message="提现记录不存在")
    if app.status != WithdrawStatus.PENDING:
        raise AppException(code=ErrorCode.WITHDRAW_AUDIT_DUPLICATE, message="该提现已处理, 不可重复审核")

    now = datetime.now(timezone.utc)

    if approved:
        app.status = WithdrawStatus.COMPLETED
        app.audit_remark = remark or "审核通过"
    else:
        # 审核拒绝 → 退回余额
        idempotent_key = generate_idempotent_key("withdraw.reject", app.user_id, withdraw_id)
        await balance_service.wallet_balance_increase(
            session=session,
            user_id=app.user_id,
            order_id=None,
            amount=app.amount,
            log_type=PaymentLogType.WITHDRAW_REJECT,
            idempotent_key=idempotent_key,
            remark=f"提现 {withdraw_id} 审核拒绝, 退回 {app.amount}",
        )
        app.status = WithdrawStatus.REJECTED
        app.audit_remark = remark or "审核拒绝"
        logger.info(f"提现审核拒绝+退款: withdraw={withdraw_id} user={app.user_id} amount={app.amount}")

    app.audited_at = now
    session.add(app)
    await session.commit()
    await session.refresh(app)

    return app


# ══════════════════════════════════════════════
# 提现记录查询
# ══════════════════════════════════════════════


async def list_user_withdraws(
    session: AsyncSession,
    user_id: int,
    status: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[WithdrawApplication], int]:
    """用户本人提现记录 (PRD 4.10)。"""
    stmt = select(WithdrawApplication).where(WithdrawApplication.user_id == user_id)
    cnt_stmt = select(func.count(WithdrawApplication.id)).where(WithdrawApplication.user_id == user_id)
    if status is not None:
        stmt = stmt.where(WithdrawApplication.status == status)
        cnt_stmt = cnt_stmt.where(WithdrawApplication.status == status)

    total_result = await session.execute(cnt_stmt)
    total = total_result.scalar() or 0
    stmt = stmt.order_by(WithdrawApplication.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    return list(result.scalars().all()), total


async def list_all_withdraws(
    session: AsyncSession,
    status: Optional[int] = None,
    user_id: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[WithdrawApplication], int]:
    """运营端全量提现记录。"""
    stmt = select(WithdrawApplication)
    cnt_stmt = select(func.count(WithdrawApplication.id))
    if status is not None:
        stmt = stmt.where(WithdrawApplication.status == status)
        cnt_stmt = cnt_stmt.where(WithdrawApplication.status == status)
    if user_id:
        stmt = stmt.where(WithdrawApplication.user_id == user_id)
        cnt_stmt = cnt_stmt.where(WithdrawApplication.user_id == user_id)

    total_result = await session.execute(cnt_stmt)
    total = total_result.scalar() or 0
    stmt = stmt.order_by(WithdrawApplication.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    return list(result.scalars().all()), total
