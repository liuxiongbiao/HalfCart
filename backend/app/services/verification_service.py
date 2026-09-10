"""
核销结算与阶梯退款服务
======================
PRD 4.5 核销结算: 6位核销码 → 验证 → 冻结转钱包 (扣 3% 服务费)
PRD 4.6 阶梯退款: GATHERING全额 / PURCHASING扣20%违约金 / DELIVERING禁止

约束:
  - 所有资金操作 → balance_service
  - 所有状态变更 → order_service
  - 订单级分布式锁 + 幂等
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.mq_client import publish_order_notify
from app.core.redis_client import RedisLock
from app.middleware.exception_handler import AppException
from app.models.order import Order
from app.models.order_participant import OrderParticipant
from app.models.verification_code import VerificationCode
from app.schemas.common import ErrorCode
from app.services import balance_service, order_service
from app.utils.constants import OrderStatus, OrderType, ParticipantPayStatus, ParticipantRole, PaymentLogType

logger = logging.getLogger(__name__)

VERIFY_LOCK_THRESHOLD = settings.VERIFICATION_CODE_LOCK_THRESHOLD   # 5
VERIFY_LOCK_MINUTES = settings.VERIFICATION_CODE_LOCK_MINUTES        # 10
FEE_RATE = Decimal(str(settings.PLATFORM_SERVICE_FEE_RATE))          # 0.03


# ══════════════════════════════════════════════
# 核销码管理
# ══════════════════════════════════════════════


def generate_verify_code() -> str:
    """生成6位数字核销码 (PRD 4.5)。"""
    return str(secrets.randbelow(1000000)).zfill(6)


async def create_verification_code(session: AsyncSession, order_id: int) -> VerificationCode:
    """为订单创建唯一核销码 (幂等)。"""
    result = await session.execute(
        select(VerificationCode).where(VerificationCode.order_id == order_id)
    )
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    vc = VerificationCode(order_id=order_id, code=generate_verify_code())
    session.add(vc)
    await session.flush()
    return vc


async def verify_code(session: AsyncSession, order_id: int, code: str) -> VerificationCode:
    """
    验证核销码 (PRD 4.5 核销安全)。

    Returns: VerificationCode (校验通过)
    Raises: AppException (码错误/已使用/已锁定)
    """
    result = await session.execute(
        select(VerificationCode).where(VerificationCode.order_id == order_id)
    )
    vc = result.scalar_one_or_none()
    if not vc:
        raise AppException(code=ErrorCode.VERIFY_ORDER_STATUS_INVALID, message="该订单未生成核销码")

    # 锁定检查
    if vc.locked_until:
        lock_time = vc.locked_until
        if lock_time.tzinfo is None:
            lock_time = lock_time.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if now < lock_time:
            remaining = max(0, int((lock_time - now).total_seconds() / 60))
            raise AppException(
                code=ErrorCode.VERIFY_CODE_LOCKED,
                message=f"核销码已锁定, 剩余 {remaining} 分钟 (连续错误{VERIFY_LOCK_THRESHOLD}次)",
            )

    if vc.is_used:
        raise AppException(code=ErrorCode.VERIFY_CODE_USED, message="核销码已使用")

    if vc.code != code:
        vc.error_count += 1
        if vc.error_count >= VERIFY_LOCK_THRESHOLD:
            vc.locked_until = datetime.now(timezone.utc) + timedelta(minutes=VERIFY_LOCK_MINUTES)
            session.add(vc)
            await session.commit()
            raise AppException(
                code=ErrorCode.VERIFY_CODE_LOCKED,
                message=f"核销码连续错误{VERIFY_LOCK_THRESHOLD}次, 已锁定{VERIFY_LOCK_MINUTES}分钟",
            )
        session.add(vc)
        await session.commit()
        remaining = VERIFY_LOCK_THRESHOLD - vc.error_count
        raise AppException(
            code=ErrorCode.VERIFY_CODE_ERROR,
            message=f"核销码错误, 还剩 {remaining} 次机会",
        )

    # 校验通过
    vc.is_used = 1
    vc.used_at = datetime.now(timezone.utc)
    vc.error_count = 0
    session.add(vc)
    return vc


# ══════════════════════════════════════════════
# 核销结算
# ══════════════════════════════════════════════


async def settle_order(
    session: AsyncSession,
    operator_id: int,
    order_id: int,
    verify_code_input: str,
    *,
    trace_id: Optional[str] = None,
) -> dict:
    """
    核销结算 — 验证核销码 → 冻结转钱包 (扣3%服务费) → 订单完成 (PRD 4.5)。

    Args:
        operator_id: 操作人 (应为团长)
        order_id:    订单ID
        verify_code_input: 6位核销码
    """
    lock = RedisLock(f"order:verify:{order_id}", timeout=15)
    async with lock as acquired:
        if not acquired:
            raise AppException(code=ErrorCode.RATE_LIMITED, message="核销操作繁忙, 请稍后重试")

        order = await _get_order(session, order_id)

        # 幂等: 已核销订单直接返回成功 (PRD 5.3)
        if order.status == OrderStatus.FINISHED:
            return {"order": order, "settlement_log": None, "fee_log": None, "already_settled": True}

        if order.status != OrderStatus.DELIVERING:
            raise AppException(
                code=ErrorCode.VERIFY_ORDER_STATUS_INVALID,
                message=f"仅待交接订单可核销, 当前状态: {OrderStatus.label(order.status)}",
            )
        if order.creator_id != operator_id:
            raise AppException(code=ErrorCode.ORDER_CREATOR_ONLY, message="仅团长可进行核销操作")

        # 验证核销码
        await verify_code(session, order_id, verify_code_input)

        # 计算金额
        total_amount = order.total_amount  # 所有拼友支付总和
        service_fee = (total_amount * FEE_RATE).quantize(Decimal("0.01"))
        income = total_amount - service_fee

        # 执行结算 (冻结转钱包 + 扣服务费) — 幂等: 同订单同一次结算
        idempotent_key = f"settle:{order_id}"
        settlement_log, fee_log = await balance_service.frozen_to_wallet_settlement(
            session=session,
            user_id=order.creator_id,
            order_id=order_id,
            total_amount=total_amount,
            idempotent_key=idempotent_key,
        )

        # 状态流转: DELIVERING → FINISHED
        order = await order_service.transition_to_finished(session, order_id, trace_id)

        # MQ 通知
        await publish_order_notify(
            order_id=order_id,
            from_status=OrderStatus.DELIVERING,
            to_status=OrderStatus.FINISHED,
            affected_user_ids=[order.creator_id],
            trace_id=trace_id,
        )

        # 查询团长结算后余额
        from app.models.user import User
        creator = await session.get(User, order.creator_id)

        logger.info(
            f"核销结算完成: order={order_id} total={total_amount} "
            f"fee={service_fee} income={income}"
        )

        return {
            "order": order,
            "settlement_log": settlement_log,
            "fee_log": fee_log,
            "total_amount": total_amount,
            "service_fee": service_fee,
            "income": income,
            "wallet_after": str(creator.wallet_balance) if creator else "0",
            "already_settled": False,
        }


# ══════════════════════════════════════════════
# 阶梯退款
# ══════════════════════════════════════════════


async def refund_order(
    session: AsyncSession,
    user_id: int,
    order_id: int,
    *,
    trace_id: Optional[str] = None,
) -> dict:
    """
    拼友申请退款 — 阶梯退款 (PRD 4.6)。

    规则:
      GATHERING(0) → 全额退款, 无违约金
      PURCHASING(1) → 扣20%违约金→团长, 80%退回拼友
      DELIVERING(2)+ → 禁止主动退款
    """
    lock = RedisLock(f"order:refund:{order_id}", timeout=15)
    async with lock as acquired:
        if not acquired:
            raise AppException(code=ErrorCode.RATE_LIMITED, message="退款操作繁忙, 请稍后重试")

        order = await _get_order(session, order_id)
        _require_normal(order)

        # 校验状态
        if order.status not in (OrderStatus.GATHERING, OrderStatus.PURCHASING):
            raise AppException(
                code=ErrorCode.REFUND_NOT_ALLOWED,
                message=f"订单状态 {OrderStatus.label(order.status)} 不允许主动退款",
            )

        # 校验拼友身份
        participant = await _get_participant(session, order_id, user_id)
        if participant.role != ParticipantRole.PARTICIPANT:
            raise AppException(code=ErrorCode.REFUND_NOT_ALLOWED, message="仅拼友可申请退款, 团长不可退款")
        if participant.pay_status == ParticipantPayStatus.REFUNDED:
            return {"refund_amount": participant.refund_amount, "order": order, "already_refunded": True}

        pay_amount = participant.pay_amount
        is_gathering = order.status == OrderStatus.GATHERING

        # 计算退款比例 (PRD 4.6)
        if is_gathering:
            refund_ratio = Decimal("1.0")
            penalty_ratio = Decimal("0.0")
        else:
            refund_ratio = Decimal("0.80")   # 退80%
            penalty_ratio = Decimal("0.20")  # 扣20%违约金

        refund_amount = (pay_amount * refund_ratio).quantize(Decimal("0.01"))
        penalty_amount = (pay_amount * penalty_ratio).quantize(Decimal("0.01"))

        base_key = f"refund:{order_id}:{user_id}"

        # Step 1: 从团长冻结扣减全额退款本金
        await balance_service.freeze_balance_decrease(
            session=session,
            user_id=order.creator_id,
            order_id=order_id,
            amount=pay_amount,
            idempotent_key=f"{base_key}:freeze_decr",
            remark=f"订单 {order_id} 拼友 {user_id} 退款解冻 (比例{refund_ratio})",
        )

        # Step 2: 退款本金退回拼友钱包 — 失败补偿
        try:
            wallet_log = await balance_service.wallet_balance_increase(
                session=session, user_id=user_id, order_id=order_id,
                amount=refund_amount, log_type=PaymentLogType.REFUND_UNFREEZE,
                idempotent_key=f"{base_key}:wallet",
                remark=f"订单 {order_id} 退款 {refund_amount} (比例{refund_ratio})",
            )
        except Exception:
            await balance_service.freeze_balance_increase(
                session=session, user_id=order.creator_id, order_id=order_id,
                amount=pay_amount,
                idempotent_key=f"{base_key}:compensate",
                remark=f"退款失败, 补偿恢复冻结",
            )
            raise

        # Step 3: 违约金→团长钱包 (仅PURCHASING)
        if penalty_amount > 0:
            await balance_service.wallet_balance_increase(
                session=session,
                user_id=order.creator_id,
                order_id=order_id,
                amount=penalty_amount,
                log_type=PaymentLogType.PENALTY_INCOME,
                idempotent_key=f"{base_key}:penalty",
                remark=f"订单 {order_id} 拼友 {user_id} 退出违约金 {penalty_amount}",
            )

        # 更新参与人记录
        participant.pay_status = ParticipantPayStatus.REFUNDED
        participant.refund_amount = refund_amount
        participant.refund_ratio = refund_ratio
        participant.refund_reason = f"{OrderStatus.label(order.status)}主动退出"
        session.add(participant)

        # 人数 -1
        order.current_people = max(1, order.current_people - 1)
        session.add(order)

        # ── 信用分扣除 (PRD 4.6) ──
        from app.models.user import User
        participant_user = await session.get(User, user_id)
        if participant_user:
            credit_penalty = 1 if is_gathering else 3  # GATHERING: -1, PURCHASING: -3
            participant_user.credit_score = max(0, participant_user.credit_score - credit_penalty)
            session.add(participant_user)
            logger.info(f"信用分扣减: user={user_id} penalty=-{credit_penalty} new={participant_user.credit_score}")

        # ── PURCHASING→GATHERING 逆向流转 (PRD 4.2: 退款后未满员) ──
        if not is_gathering and order.current_people < order.total_people:
            old_status = order.status
            order.status = OrderStatus.GATHERING
            session.add(order)
            logger.info(
                f"订单逆向流转: order={order_id} status PURCHASING→GATHERING "
                f"(退款后 {order.current_people}/{order.total_people})"
            )
            # ES 同步
            try:
                await order_service._send_order_sync(
                    order.id, "order.status_changed",
                    {"order_id": order.id, "action": "upsert", "build_doc": True}, trace_id
                )
            except Exception:
                pass

        await session.commit()

        # MQ 通知
        await publish_order_notify(
            order_id=order_id,
            from_status=order.status,
            to_status=order.status,
            affected_user_ids=[order.creator_id, user_id],
            trace_id=trace_id,
        )

        logger.info(
            f"退款完成: order={order_id} user={user_id} "
            f"refund={refund_amount} penalty={penalty_amount} "
            f"status={OrderStatus.label(order.status)}"
        )

        return {
            "order": order,
            "refund_amount": refund_amount,
            "penalty_amount": penalty_amount,
            "refund_ratio": str(refund_ratio),
            "wallet_after": str(wallet_log.balance_after),
            "already_refunded": False,
        }


# ══════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════


async def _get_order(session: AsyncSession, order_id: int) -> Order:
    o = await session.get(Order, order_id)
    if not o:
        raise AppException(code=ErrorCode.ORDER_NOT_FOUND, message=f"订单不存在: {order_id}")
    return o


async def _get_participant(session: AsyncSession, order_id: int, user_id: int) -> OrderParticipant:
    result = await session.execute(
        select(OrderParticipant).where(
            OrderParticipant.order_id == order_id,
            OrderParticipant.user_id == user_id,
        )
    )
    p = result.scalar_one_or_none()
    if not p:
        raise AppException(code=ErrorCode.ORDER_NOT_MEMBER, message="你不是该订单的参与人")
    return p


def _require_normal(order: Order) -> None:
    if order.order_type != OrderType.NORMAL:
        raise AppException(code=ErrorCode.ORDER_STATUS_INVALID, message="仅普通拼单支持退款")
