"""
拼单参与服务 — 加入/退出
========================
PRD 4.4: 双视角隔离 — 团长无加入入口, 拼友无核销入口。
PRD 4.6: 阶梯退款 — GATHERING全额退款, PURCHASING扣违约金(本模块仅处理GATHERING退出)。

强制约束:
  - 所有资金操作 → balance_service (禁止直接SQL)
  - 所有状态流转 → order_service (禁止直接改status)
  - 订单级分布式锁 → RedisLock("order:join:{order_id}")
  - 同一用户同一订单仅一条参与记录 → DB UNIQUE (order_id, user_id)
"""

import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.mq_client import publish_order_notify
from app.core.redis_client import RedisLock
from app.middleware.exception_handler import AppException
from app.models.order import Order
from app.models.order_participant import OrderParticipant
from app.models.user import User
from app.schemas.common import ErrorCode
from app.services import balance_service, order_service
from app.utils.constants import OrderStatus, OrderType, ParticipantPayStatus, ParticipantRole, PaymentLogType
from app.utils.idempotent import generate_idempotent_key

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 用户加入拼单
# PRD 4.4: 拼友在雷达大厅点击「立即支付」→ 扣钱包 → 冻团长的冻结余额
# ══════════════════════════════════════════════


async def join_order(
    session: AsyncSession,
    user_id: int,
    order_id: int,
    *,
    trace_id: Optional[str] = None,
) -> dict:
    """
    用户加入拼单 (PRD 4.4 拼友视角 - 立即支付)。

    完整流程:
      1. 校验订单: 存在/普通拼单/状态GATHERING/未过期
      2. 校验用户: 非团长/未参与/钱包余额≥人均价格
      3. 订单级分布式锁 (防并发超卖)
      4. 事务: 扣钱包→冻余额→写参与人→更新人数→满员流转
      5. MQ: 状态变更通知+ES同步

    Args:
        session:  数据库会话 (事务边界由调用方管理)
        user_id:  当前用户ID
        order_id: 订单ID

    Returns:
        {"order": Order, "log": PaymentLog, "wallet_after": Decimal,
         "transitioned": bool (是否满员自动流转)}

    Raises:
        AppException: 各种校验/资金/状态异常
    """
    # ── Step 1: 加分布式锁 ──────────────────
    lock = RedisLock(f"order:join:{order_id}", timeout=15)
    async with lock as acquired:
        if not acquired:
            raise AppException(
                code=ErrorCode.RATE_LIMITED,
                message="拼单繁忙，请稍后重试",
            )

        # ── Step 2: 校验订单 ──────────────────
        order = await _get_order_or_fail(session, order_id)
        _require_normal_order(order)            # 仅普通拼单
        _require_status(order, OrderStatus.GATHERING)  # 仅集资中
        _require_not_expired(order)             # 未过期
        _require_not_creator(order, user_id)    # 非团长 (PRD 3.1)
        _require_not_full(order)                # 名额未满

        # ── Step 3: 校验用户是否已参与 ─────────
        existing = await _find_participant(session, order_id, user_id)
        if existing:
            # 幂等: 已参与返回成功, 不重复扣钱 (PRD 5.3)
            logger.info(f"幂等: user={user_id} 已参与 order={order_id}, 直接返回")
            return {
                "order": order,
                "log": None,
                "wallet_after": None,
                "transitioned": False,
                "already_joined": True,
            }

        # ── Step 4: 校验用户余额 ──────────────
        user = await _get_user_or_fail(session, user_id)
        price = order.price_per_person
        if user.wallet_balance < price:
            raise AppException(
                code=ErrorCode.PAYMENT_WALLET_INSUFFICIENT,
                message=f"钱包余额不足: 当前 {user.wallet_balance}, 需要 {price}",
            )

        # ── Step 5: 幂等键 ────────────────────
        idempotent_key = generate_idempotent_key(
            "join.order",
            user_id,
            order_id,
        )

        # ── Step 6: 执行资金操作 ──────────────
        # 6a. 扣减拼友钱包余额 (balance_service 内部 commit)
        wallet_log = await balance_service.wallet_balance_decrease(
            session=session,
            user_id=user_id,
            amount=price,
            idempotent_key=f"{idempotent_key}:wallet",
            remark=f"加入拼单 {order_id} 支付人均 {price}",
        )

        # 6b. 增加团长冻结余额 — 失败则补偿退回钱包
        try:
            freeze_log = await balance_service.freeze_balance_increase(
                session=session,
                user_id=order.creator_id,
                order_id=order_id,
                amount=price,
                idempotent_key=f"{idempotent_key}:freeze",
                remark=f"订单 {order_id} 拼友 {user_id} 支付冻结",
            )
        except Exception:
            logger.exception(f"冻结余额失败, 执行补偿退款: user={user_id}, order={order_id}, amount={price}")
            # 补偿: 退回钱包扣减
            await balance_service.wallet_balance_increase(
                session=session, user_id=user_id, order_id=order_id,
                amount=price, log_type=PaymentLogType.REFUND_UNFREEZE,
                idempotent_key=f"{idempotent_key}:compensate",
                remark=f"加入拼单 {order_id} 冻结失败, 补偿退回",
            )
            raise

        # 6c. 新增参与人记录
        participant = OrderParticipant(
            order_id=order_id, user_id=user_id,
            role=ParticipantRole.PARTICIPANT,
            pay_status=ParticipantPayStatus.PAID, pay_amount=price,
        )
        session.add(participant)
        order.current_people += 1
        session.add(order)
        await session.commit()

        # 6e. 满员检查 → 自动流转 GATHERING→PURCHASING
        transitioned = False
        if order.current_people >= order.total_people:
            order = await order_service.transition_to_purchasing(
                session, order_id, trace_id
            )
            transitioned = True
            logger.info(
                f"订单满员自动流转: order={order_id} "
                f"{order.current_people}/{order.total_people} → PURCHASING"
            )

        # ── Step 7: MQ 通知 ───────────────────
        await publish_order_notify(
            order_id=order_id,
            from_status=OrderStatus.GATHERING,
            to_status=order.status,
            affected_user_ids=[order.creator_id, user_id],
            trace_id=trace_id,
        )

        logger.info(
            f"加入拼单成功: user={user_id} order={order_id} "
            f"amount={price} people={order.current_people}/{order.total_people} "
            f"transitioned={transitioned}"
        )

        return {
            "order": order,
            "log": wallet_log,
            "wallet_after": wallet_log.balance_after,
            "transitioned": transitioned,
            "already_joined": False,
        }


# ══════════════════════════════════════════════
# 购买现货订单 (PRD 4.8)
# 拼友在雷达大厅点击现货 → 支付 → 获取核销码
# ══════════════════════════════════════════════


async def join_spot_order(
    session: AsyncSession,
    user_id: int,
    order_id: int,
    *,
    trace_id: Optional[str] = None,
) -> dict:
    """
    购买现货订单 (PRD 4.8): 支付后直接获取核销码, 线下自提。

    与 join_order 的区别:
      - 校验 is_spot=true + status=DELIVERING (非 GATHERING)
      - 不校验满员 (现货 total_people=1, 一人买即成交)
      - 返回核销码供线下出示
    """
    lock = RedisLock(f"order:join:{order_id}", timeout=15)
    async with lock as acquired:
        if not acquired:
            raise AppException(code=ErrorCode.RATE_LIMITED, message="操作繁忙, 请稍后重试")

        order = await _get_order_or_fail(session, order_id)

        # 现货专属校验
        if not order.is_spot:
            raise AppException(
                code=ErrorCode.ORDER_STATUS_INVALID,
                message="仅现货订单支持此操作",
            )
        if order.status != OrderStatus.DELIVERING:
            raise AppException(
                code=ErrorCode.ORDER_STATUS_INVALID,
                message="该现货已过期或已售出",
            )
        _require_not_creator(order, user_id)
        _require_not_expired(order)

        # 幂等: 已购买直接返回核销码
        existing = await _find_participant(session, order_id, user_id)
        if existing:
            from app.models.verification_code import VerificationCode
            vc_result = await session.execute(
                select(VerificationCode).where(VerificationCode.order_id == order_id)
            )
            vc = vc_result.scalar_one_or_none()
            logger.info(f"幂等: user={user_id} 已购买现货 order={order_id}")
            return {
                "order": order,
                "verify_code": vc.code if vc else "",
                "already_bought": True,
            }

        user = await _get_user_or_fail(session, user_id)
        price = order.price_per_person
        if user.wallet_balance < price:
            raise AppException(
                code=ErrorCode.PAYMENT_WALLET_INSUFFICIENT,
                message=f"钱包余额不足: 当前 {user.wallet_balance}, 需要 {price}",
            )

        idempotent_key = generate_idempotent_key("buy.spot", user_id, order_id)

        # 扣钱包
        wallet_log = await balance_service.wallet_balance_decrease(
            session=session, user_id=user_id, amount=price,
            idempotent_key=f"{idempotent_key}:wallet",
            remark=f"购买现货 {order_id} 支付 {price}",
        )

        # 冻结到团长
        try:
            await balance_service.freeze_balance_increase(
                session=session, user_id=order.creator_id, order_id=order_id,
                amount=price,
                idempotent_key=f"{idempotent_key}:freeze",
                remark=f"现货 {order_id} 买家 {user_id} 支付冻结",
            )
        except Exception:
            logger.exception(f"冻结余额失败(现货), 执行补偿退款: user={user_id}, order={order_id}, amount={price}")
            await balance_service.wallet_balance_increase(
                session=session, user_id=user_id, order_id=order_id,
                amount=price, log_type=PaymentLogType.REFUND_UNFREEZE,
                idempotent_key=f"{idempotent_key}:compensate",
                remark=f"购买现货 {order_id} 冻结失败, 补偿退回",
            )
            raise

        # 新增参与人
        participant = OrderParticipant(
            order_id=order_id, user_id=user_id,
            role=ParticipantRole.PARTICIPANT,
            pay_status=ParticipantPayStatus.PAID, pay_amount=price,
        )
        session.add(participant)
        order.current_people += 1
        session.add(order)
        await session.commit()

        # 获取核销码 (现货创建时已生成)
        from app.models.verification_code import VerificationCode
        vc_result = await session.execute(
            select(VerificationCode).where(VerificationCode.order_id == order_id)
        )
        vc = vc_result.scalar_one_or_none()

        logger.info(
            f"购买现货成功: user={user_id} order={order_id} "
            f"amount={price} verify_code={vc.code if vc else 'N/A'}"
        )

        return {
            "order": order,
            "wallet_after": wallet_log.balance_after,
            "verify_code": vc.code if vc else "",
            "already_bought": False,
        }


# ══════════════════════════════════════════════
# 用户退出拼单
# PRD 4.6: GATHERING 全额退款, frozen→wallet
# ══════════════════════════════════════════════


async def quit_order(
    session: AsyncSession,
    user_id: int,
    order_id: int,
    *,
    trace_id: Optional[str] = None,
) -> dict:
    """
    用户退出拼单 — 仅 GATHERING 状态 (PRD 4.6)。

    流程:
      1. 校验: 订单GATHERING/用户是该订单普通拼友
      2. 订单级分布式锁
      3. 事务: 团长frozen→用户wallet (全额退款)
      4. 更新参与人记录为已退款
      5. 订单人数 -1

    Args:
        session:  数据库会话
        user_id:  当前用户ID
        order_id: 订单ID

    Returns:
        {"refund_amount": Decimal, "wallet_after": Decimal, "order": Order}
    """
    lock = RedisLock(f"order:quit:{order_id}", timeout=15)
    async with lock as acquired:
        if not acquired:
            raise AppException(
                code=ErrorCode.RATE_LIMITED,
                message="操作繁忙，请稍后重试",
            )

        order = await _get_order_or_fail(session, order_id)
        _require_status(order, OrderStatus.GATHERING)
        _require_not_creator(order, user_id)

        # 校验是否为该订单拼友
        participant = await _find_participant(session, order_id, user_id)
        if not participant:
            raise AppException(
                code=ErrorCode.ORDER_NOT_MEMBER,
                message=f"你不是该订单的拼友: order={order_id}",
            )
        if participant.pay_status == ParticipantPayStatus.REFUNDED:
            # 幂等: 已退款, 直接返回
            logger.info(f"幂等: user={user_id} 已退出 order={order_id}")
            return {"refund_amount": participant.refund_amount, "wallet_after": None, "order": order, "already_quit": True}

        refund_amount = participant.pay_amount
        idempotent_key = generate_idempotent_key(
            "quit.order",
            user_id,
            order_id,
        )

        # 退款: 团长冻结余额 → 用户钱包余额
        await balance_service.freeze_balance_decrease(
            session=session,
            user_id=order.creator_id,
            order_id=order_id,
            amount=refund_amount,
            idempotent_key=f"{idempotent_key}:freeze_decr",
            remark=f"拼友 {user_id} 退出订单 {order_id} 退款解冻",
        )

        # 退回钱包 — 失败则补偿恢复冻结
        try:
            wallet_log = await balance_service.wallet_balance_increase(
                session=session, user_id=user_id, order_id=order_id,
                amount=refund_amount, log_type=PaymentLogType.REFUND_UNFREEZE,
                idempotent_key=f"{idempotent_key}:wallet",
                remark=f"退出拼单 {order_id} 全额退款 {refund_amount} (PRD 4.6)",
            )
        except Exception:
            logger.exception(f"退款到钱包失败, 执行补偿恢复冻结: user={user_id}, order={order_id}, amount={refund_amount}")
            await balance_service.freeze_balance_increase(
                session=session, user_id=order.creator_id, order_id=order_id,
                amount=refund_amount,
                idempotent_key=f"{idempotent_key}:compensate",
                remark=f"退出拼单 {order_id} 退钱包失败, 补偿恢复冻结",
            )
            raise

        # 更新参与人记录
        participant.pay_status = ParticipantPayStatus.REFUNDED
        participant.refund_amount = refund_amount
        participant.refund_ratio = Decimal("1.0")  # 全额
        participant.refund_reason = "GATHERING退出"
        session.add(participant)

        # 订单人数 -1
        order.current_people = max(1, order.current_people - 1)
        session.add(order)

        # ── 信用分扣除 (PRD 4.6: GATHERING退出 -1分) ──
        from app.models.user import User
        quitter = await session.get(User, user_id)
        if quitter:
            quitter.credit_score = max(0, quitter.credit_score - 1)
            session.add(quitter)
            logger.info(f"信用分扣减: user={user_id} -1 (GATHERING退出) new={quitter.credit_score}")

        await session.commit()

        # MQ 通知
        await publish_order_notify(
            order_id=order_id,
            from_status=OrderStatus.GATHERING,
            to_status=OrderStatus.GATHERING,  # 状态未变
            affected_user_ids=[order.creator_id, user_id],
            trace_id=trace_id,
        )

        logger.info(
            f"退出拼单成功: user={user_id} order={order_id} "
            f"refund={refund_amount} people={order.current_people}"
        )

        return {
            "refund_amount": refund_amount,
            "wallet_after": wallet_log.balance_after,
            "order": order,
            "already_quit": False,
        }


# ══════════════════════════════════════════════
# 辅助校验函数
# ══════════════════════════════════════════════


async def _get_order_or_fail(session: AsyncSession, order_id: int) -> Order:
    o = await session.get(Order, order_id)
    if not o:
        raise AppException(code=ErrorCode.ORDER_NOT_FOUND, message=f"订单不存在: {order_id}")
    return o


async def _get_user_or_fail(session: AsyncSession, user_id: int) -> User:
    u = await session.get(User, user_id)
    if not u:
        raise AppException(code=ErrorCode.PAYMENT_USER_NOT_FOUND, message=f"用户不存在: {user_id}")
    return u


async def _find_participant(
    session: AsyncSession, order_id: int, user_id: int
) -> Optional[OrderParticipant]:
    result = await session.execute(
        select(OrderParticipant).where(
            OrderParticipant.order_id == order_id,
            OrderParticipant.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


def _require_normal_order(order: Order) -> None:
    if order.order_type != OrderType.NORMAL:
        raise AppException(
            code=ErrorCode.ORDER_STATUS_INVALID,
            message="仅普通拼单允许加入, 现货订单请直接购买",
        )


def _require_status(order: Order, expected: int) -> None:
    if order.status != expected:
        raise AppException(
            code=ErrorCode.ORDER_STATUS_INVALID,
            message=f"订单状态为 {OrderStatus.label(order.status)}, 非 {OrderStatus.label(expected)}",
        )


def _require_not_expired(order: Order) -> None:
    if order_service.is_order_expired(order):
        raise AppException(
            code=ErrorCode.ORDER_DEADLINE_EXPIRED,
            message="该订单已过截单时间",
        )


def _require_not_creator(order: Order, user_id: int) -> None:
    if order.creator_id == user_id:
        raise AppException(
            code=ErrorCode.ORDER_SELF_JOIN_DENIED,
            message="团长本人不能加入自己的订单 (PRD 3.1)",
        )


def _require_not_full(order: Order) -> None:
    if order.current_people >= order.total_people:
        raise AppException(
            code=ErrorCode.ORDER_FULL,
            message=f"拼单已满员 ({order.current_people}/{order.total_people})",
        )
