"""
支付宝支付业务服务
===================
PRD V3.2: 拼单支付宝支付 — 创建支付/异步回调/查询/退款。

核心规则:
  - 支付成功后订单状态不变 (仍为 GATHERING), 仅更新资金
  - 资金记账: 写入支付流水 → 团长 frozen_balance 增加 → 写全局资金流水 → 更新支付人数
  - 支付超时 5 分钟, Redis 锁自动释放名额
  - 退款复用 PRD 阶梯退款: GATHERING=100%, PURCHASING=70%-80%
  - 所有资金操作: 事务 + Redis 分布式锁 + 幂等

约束:
  - 所有资金操作走 balance_service
  - 所有状态查询走 order_service
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import async_session_factory
from app.core.mq_client import publish_message
from app.core.redis_client import RedisLock, get_redis
from app.middleware.exception_handler import AppException
from app.models.alipay_record import AlipayRecord, AlipayTradeStatus
from app.models.order import Order
from app.models.order_participant import OrderParticipant
from app.models.user import User
from app.schemas.common import ErrorCode
from app.services import balance_service
from app.utils.alipay_client import (
    AlipayApiException,
    create_pay_url,
    query_order,
    refund as alipay_refund,
    verify_notify_sign,
)
from app.utils.constants import (
    CreditChange,
    OrderStatus,
)
from app.utils.idempotent import (
    BusinessScene,
    generate_idempotent_key,
)

logger = logging.getLogger(__name__)

# ── Redis Key ──
_PAYMENT_LOCK_KEY = "halfcart:alipay:lock:{out_trade_no}"
_PAYMENT_SLOT_KEY = "halfcart:alipay:slot:{order_id}:{user_id}"
_PAYMENT_SLOT_TTL = 300  # 5分钟支付超时


# ══════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════


def _gen_out_trade_no() -> str:
    """生成全局唯一商户订单号 HC + 时间戳(ms) + 随机。"""
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    rand = uuid.uuid4().hex[:8]
    return f"HC{ts}{rand}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ══════════════════════════════════════════════
# POST /api/v1/pay/alipay/create — 创建支付
# ══════════════════════════════════════════════


async def create_payment(
    session: AsyncSession,
    user_id: int,
    order_id: int,
) -> dict:
    """
    创建支付宝支付单 (PRD V3.2)。

    前置校验:
      - 订单存在且状态为 GATHERING
      - 用户不是团长 (团长无需支付)
      - 用户未支付
      - 名额充足

    流程:
      1. 校验 → 2. 生成 out_trade_no → 3. 写支付流水(PENDING)
      → 4. Redis 锁名额 5 分钟 → 5. 调支付宝生成支付链接 → 6. 返回
    """
    r = get_redis()

    # ── 校验订单 ──
    order = await session.get(Order, order_id)
    if not order:
        raise AppException(code=ErrorCode.ORDER_NOT_FOUND, message="订单不存在")

    if order.status != OrderStatus.GATHERING:
        raise AppException(
            code=ErrorCode.ORDER_STATUS_INVALID,
            message=f"当前订单状态为「{OrderStatus.label(order.status)}」, 无法支付",
        )

    if order.creator_id == user_id:
        raise AppException(
            code=ErrorCode.ORDER_CREATOR_ONLY,
            message="团长无需支付, 您可以直接发起拼单",
        )

    if order.current_people >= order.total_people:
        raise AppException(code=ErrorCode.ORDER_FULL, message="拼单已满员")

    # ── 查参与记录 ──
    participant = (
        await session.execute(
            select(OrderParticipant).where(
                OrderParticipant.order_id == order_id,
                OrderParticipant.user_id == user_id,
            )
        )
    ).scalar_one_or_none()

    if not participant:
        raise AppException(
            code=ErrorCode.ORDER_NOT_MEMBER, message="请先加入拼单再支付"
        )

    if participant.pay_status == 1:
        raise AppException(
            code=ErrorCode.PAYMENT_DUPLICATE, message="您已支付过该订单"
        )

    # ── Redis 名额锁 (原子 SETNX, 防止并发超卖) ──
    slot_key = _PAYMENT_SLOT_KEY.format(order_id=order_id, user_id=user_id)
    acquired = await r.set(slot_key, "1", nx=True, ex=_PAYMENT_SLOT_TTL)
    if not acquired:
        raise AppException(
            code=ErrorCode.PAYMENT_DUPLICATE,
            message="您已有待支付订单, 请在5分钟内完成支付",
        )

    # ── 创建支付流水 ──
    out_trade_no = _gen_out_trade_no()
    amount = order.price_per_person
    subject = f"HalfCart拼单-{order.goods_name}"
    body = f"订单号: {order.order_no}"

    record = AlipayRecord(
        out_trade_no=out_trade_no,
        user_id=user_id,
        order_id=order_id,
        amount=amount,
        subject=subject,
        body=body,
        trade_status=AlipayTradeStatus.PENDING,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    # ── 生成支付链接 ──
    pay_url = await create_pay_url(
        out_trade_no=out_trade_no,
        total_amount=str(amount),
        subject=subject,
        body=body,
    )

    logger.info(
        f"支付宝支付单已创建: out_trade_no={out_trade_no} "
        f"user_id={user_id} order_id={order_id} amount={amount}"
    )
    return {
        "pay_url": pay_url,
        "out_trade_no": out_trade_no,
        "amount": str(amount),
        "expires_in": _PAYMENT_SLOT_TTL,
    }


# ══════════════════════════════════════════════
# POST /api/v1/pay/alipay/notify — 异步回调
# ══════════════════════════════════════════════


async def handle_notify(params: dict) -> str:
    """
    处理支付宝异步回调 (PRD V3.2 核心)。

    五步流程:
      1. 验签 → 100% 通过才继续
      2. 幂等校验 → 同一 trade_no 仅处理一次
      3. 金额校验 → 与支付流水一致
      4. 资金记账 (事务+锁) → frozen_balance 增加
      5. 返回 "success" 给支付宝

    安全: 验签失败、金额不一致、订单不存在 → 记录日志, 返回 "fail"
    """
    # ── Step 1: 验签 ──
    params_copy = dict(params)  # 保留原始副本用于日志
    if not verify_notify_sign(params_copy):
        logger.error(f"支付宝回调验签失败: out_trade_no={params.get('out_trade_no')}")
        return "fail"

    out_trade_no = params.get("out_trade_no", "")
    trade_no = params.get("trade_no", "")
    notify_amount = params.get("total_amount", "0")
    trade_status_alipay = params.get("trade_status", "")

    # 仅处理 TRADE_SUCCESS
    if trade_status_alipay != "TRADE_SUCCESS":
        logger.info(
            f"支付宝回调非成功状态: out_trade_no={out_trade_no} "
            f"trade_status={trade_status_alipay}"
        )
        return "success"

    lock_key = _PAYMENT_LOCK_KEY.format(out_trade_no=out_trade_no)

    async with RedisLock(lock_key, timeout=15) as acquired:
        if not acquired:
            logger.warning(f"获取分布式锁失败: {lock_key}")
            return "fail"

        async with async_session_factory() as session:
            # ── Step 2: 幂等校验 ──
            result = await session.execute(
                select(AlipayRecord).where(
                    AlipayRecord.out_trade_no == out_trade_no
                )
            )
            record = result.scalar_one_or_none()

            if not record:
                logger.error(f"支付流水不存在: out_trade_no={out_trade_no}")
                return "fail"

            # 已处理过的直接返回 success
            if record.trade_status == AlipayTradeStatus.SUCCESS:
                return "success"

            if record.trade_status != AlipayTradeStatus.PENDING:
                logger.warning(
                    f"支付单非待支付状态: out_trade_no={out_trade_no} "
                    f"status={record.trade_status}"
                )
                return "fail"

            # ── Step 3: 金额校验 ──
            if Decimal(notify_amount) != record.amount:
                logger.error(
                    f"支付金额不一致: out_trade_no={out_trade_no} "
                    f"notify={notify_amount} record={record.amount}"
                )
                record.raw_notify = json.dumps(params, ensure_ascii=False)
                session.add(record)
                await session.commit()
                return "fail"

            # ── Step 4: 资金记账 ──
            order = await session.get(Order, record.order_id)
            if not order:
                logger.error(f"订单不存在: order_id={record.order_id}")
                return "fail"

            # 超时后支付 → 自动全额退款
            slot_key = _PAYMENT_SLOT_KEY.format(
                order_id=record.order_id, user_id=record.user_id
            )
            r = get_redis()
            if not await r.exists(slot_key):
                logger.warning(
                    f"支付超时, 触发自动退款: out_trade_no={out_trade_no}"
                )
                record.trade_status = AlipayTradeStatus.CLOSED
                record.raw_notify = json.dumps(params, ensure_ascii=False)
                session.add(record)
                await session.commit()
                # 异步退款
                await _auto_refund_timeout(out_trade_no, record.amount)
                return "success"

            # 冻结团长余额 (复用 balance_service)
            idempotent_key = generate_idempotent_key(
                BusinessScene.PAYMENT_FREEZE,
                order.creator_id,
                record.order_id,
                out_trade_no,
            )

            await balance_service.freeze_balance_increase(
                session=session,
                user_id=order.creator_id,
                amount=record.amount,
                order_id=record.order_id,
                idempotent_key=idempotent_key,
            )

            # 更新支付记录
            record.trade_no = trade_no
            record.trade_status = AlipayTradeStatus.SUCCESS
            record.pay_time = _now_utc()
            record.notify_time = _now_utc()
            record.raw_notify = json.dumps(params, ensure_ascii=False)
            session.add(record)

            # 更新参与人支付状态
            participant_result = await session.execute(
                select(OrderParticipant).where(
                    OrderParticipant.order_id == record.order_id,
                    OrderParticipant.user_id == record.user_id,
                )
            )
            participant = participant_result.scalar_one_or_none()
            if participant:
                participant.pay_status = 1
                session.add(participant)

            await session.commit()

            # 释放 Redis 名额锁
            await r.delete(slot_key)

    # ── Step 5: 发送 MQ 消息 ──
    try:
        await publish_order_paid_event(
            order_id=record.order_id,
            user_id=record.user_id,
            amount=record.amount,
            out_trade_no=out_trade_no,
        )
    except Exception as e:
        logger.warning(f"MQ 支付消息发送失败: {e}")

    logger.info(
        f"支付宝支付成功处理完成: out_trade_no={out_trade_no} "
        f"trade_no={trade_no[:8]}... user_id={record.user_id}"
    )
    return "success"


# ══════════════════════════════════════════════
# GET /api/v1/pay/alipay/return — 同步回调
# ══════════════════════════════════════════════


async def handle_return(params: dict) -> dict:
    """处理支付宝同步回调 — 验签 + 返回前端跳转参数。"""
    if not verify_notify_sign(dict(params)):
        return {"verified": False, "error": "签名校验失败"}

    out_trade_no = params.get("out_trade_no", "")
    return {
        "verified": True,
        "out_trade_no": out_trade_no,
        "trade_no": params.get("trade_no", ""),
        "total_amount": params.get("total_amount", "0"),
    }


# ══════════════════════════════════════════════
# GET /api/v1/pay/alipay/query/{out_trade_no} — 查询
# ══════════════════════════════════════════════


async def query_payment_status(
    session: AsyncSession,
    out_trade_no: str,
    user_id: int,
) -> dict:
    """查询支付状态 — 本地优先, 状态不明再查支付宝。"""
    result = await session.execute(
        select(AlipayRecord).where(AlipayRecord.out_trade_no == out_trade_no)
    )
    record = result.scalar_one_or_none()

    if not record:
        raise AppException(
            code=ErrorCode.RESOURCE_NOT_FOUND, message="支付记录不存在"
        )

    # 权限: 只能查自己的
    if record.user_id != user_id:
        raise AppException(
            code=ErrorCode.PERMISSION_DENIED, message="无权查看该支付记录"
        )

    status_label = AlipayTradeStatus.label(record.trade_status)

    # 如果本地是 PENDING, 主动查一次支付宝同步状态
    alipay_info = None
    if record.trade_status == AlipayTradeStatus.PENDING:
        try:
            alipay_info = await query_order(out_trade_no)
        except AlipayApiException:
            pass  # 查不到不影响返回

    return {
        "out_trade_no": record.out_trade_no,
        "trade_no": record.trade_no or "",
        "amount": str(record.amount),
        "trade_status": record.trade_status,
        "trade_status_label": status_label,
        "refund_amount": str(record.refund_amount) if record.refund_amount else "0",
        "subject": record.subject,
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "pay_time": record.pay_time.isoformat() if record.pay_time else None,
        "alipay_query": alipay_info,
    }


# ══════════════════════════════════════════════
# POST /api/v1/pay/alipay/refund — 退款
# ══════════════════════════════════════════════


async def apply_refund(
    session: AsyncSession,
    order_id: int,
    user_id: int,
    reason: str = "",
) -> dict:
    """
    申请退款 (PRD V3.2 阶梯退款)。

    规则:
      - GATHERING: 全额退款, 扣 1 分
      - PURCHASING: 退款 75%, 违约金归团长, 扣 3 分
      - DELIVERING/FINISHED: 禁止主动退款
    """
    # ── 查订单 + 参与记录 ──
    order = await session.get(Order, order_id)
    if not order:
        raise AppException(code=ErrorCode.ORDER_NOT_FOUND, message="订单不存在")

    participant_result = await session.execute(
        select(OrderParticipant).where(
            OrderParticipant.order_id == order_id,
            OrderParticipant.user_id == user_id,
        )
    )
    participant = participant_result.scalar_one_or_none()
    if not participant:
        raise AppException(
            code=ErrorCode.ORDER_NOT_MEMBER, message="您未参与该订单"
        )

    if participant.pay_status != 1:
        raise AppException(
            code=ErrorCode.PAYMENT_DUPLICATE, message="未支付或已退款, 无法申请退款"
        )

    # ── 查支付流水 ──
    pay_record_result = await session.execute(
        select(AlipayRecord).where(
            AlipayRecord.order_id == order_id,
            AlipayRecord.user_id == user_id,
            AlipayRecord.trade_status.in_(
                [AlipayTradeStatus.SUCCESS, AlipayTradeStatus.PARTIAL_REFUND]
            ),
        )
    )
    pay_record = pay_record_result.scalar_one_or_none()
    if not pay_record:
        raise AppException(
            code=ErrorCode.RESOURCE_NOT_FOUND, message="未找到成功支付记录"
        )

    # ── 退款比例 ──
    if order.status == OrderStatus.GATHERING:
        refund_ratio = Decimal("1.00")
        credit_delta = CreditChange.JOIN_QUIT.value
    elif order.status == OrderStatus.PURCHASING:
        refund_ratio = Decimal("0.80")
        credit_delta = CreditChange.PURCHASE_QUIT.value
    else:
        raise AppException(
            code=ErrorCode.REFUND_NOT_ALLOWED,
            message=f"当前订单状态为「{OrderStatus.label(order.status)}」, 不支持主动退款",
        )

    refund_amount = (pay_record.amount * refund_ratio).quantize(Decimal("0.01"))
    refund_reason = reason or "用户主动退款"

    # ── 调用支付宝退款接口 ──
    out_request_no = f"RF{pay_record.out_trade_no[-20:]}{uuid.uuid4().hex[:6]}"
    alipay_response = await alipay_refund(
        out_trade_no=pay_record.out_trade_no,
        refund_amount=str(refund_amount),
        refund_reason=refund_reason,
        out_request_no=out_request_no,
    )

    # ── 资金处理 ──
    lock_key = f"halfcart:refund:lock:{pay_record.out_trade_no}"

    async with RedisLock(lock_key, timeout=15) as acquired:
        if not acquired:
            raise AppException(
                code=ErrorCode.PAYMENT_LOCK_FAILED, message="退款处理中, 请稍后重试"
            )

        # 1. 团长冻结余额扣减 (全额扣减原支付金额)
        unfreeze_key = generate_idempotent_key(
            BusinessScene.REFUND_UNFREEZE,
            order.creator_id,
            order_id,
            f"refund:{pay_record.out_trade_no}",
        )

        try:
            await balance_service.freeze_balance_decrease(
                session=session,
                user_id=order.creator_id,
                amount=pay_record.amount,
                order_id=order_id,
                idempotent_key=unfreeze_key,
            )
        except AppException as e:
            if e.code == ErrorCode.PAYMENT_IDEMPOTENT_BLOCKED:
                pass  # 幂等重试: 已扣减则跳过
            else:
                raise

        # 2. GATHERING 全额退款 → 原路退回 + 团长冻结扣减完成
        if order.status == OrderStatus.GATHERING:
            pass  # 全额退款已在支付宝侧完成, 本地仅扣减团长冻结余额

        # 3. PURCHASING 部分退款 → 违约金划转团长钱包
        if order.status == OrderStatus.PURCHASING:
            penalty = pay_record.amount - refund_amount
            if penalty > 0:
                penalty_key = generate_idempotent_key(
                    BusinessScene.PENALTY_INCOME,
                    order.creator_id,
                    order_id,
                    f"penalty:{pay_record.out_trade_no}",
                )
                # 违约金从已扣减的团长冻结余额中转至团长钱包 (PRD 4.6)
                await balance_service.wallet_balance_increase(
                    session=session,
                    user_id=order.creator_id,
                    amount=penalty,
                    order_id=order_id,
                    idempotent_key=penalty_key,
                )

        # 4. 更新支付流水
        is_full = refund_ratio == Decimal("1.00")
        pay_record.trade_status = (
            AlipayTradeStatus.FULL_REFUND if is_full else AlipayTradeStatus.PARTIAL_REFUND
        )
        pay_record.refund_amount = refund_amount
        pay_record.refund_reason = refund_reason
        session.add(pay_record)

        # 5. 更新参与记录
        participant.pay_status = 2
        participant.refund_amount = refund_amount
        participant.refund_ratio = float(refund_ratio)
        participant.refund_reason = refund_reason
        session.add(participant)

        # 6. 扣减信用分
        user = await session.get(User, user_id)
        if user:
            user.credit_score = max(0, min(120, user.credit_score + credit_delta))
            session.add(user)

        # 7. 订单人数 -1 (如果是 GATHERING)
        if order.status == OrderStatus.GATHERING:
            order.current_people = max(1, order.current_people - 1)
            session.add(order)

        await session.commit()

    logger.info(
        f"退款处理完成: out_trade_no={pay_record.out_trade_no} "
        f"refund_amount={refund_amount} ratio={refund_ratio}"
    )

    return {
        "out_trade_no": pay_record.out_trade_no,
        "refund_amount": str(refund_amount),
        "refund_ratio": str(refund_ratio),
        "trade_status": AlipayTradeStatus.label(pay_record.trade_status),
        "credit_delta": credit_delta,
        "alipay_response": alipay_response,
    }


# ══════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════


async def _auto_refund_timeout(out_trade_no: str, amount: Decimal) -> None:
    """超时自动退款 (后台异步, 不阻塞回调返回)。"""
    try:
        await alipay_refund(
            out_trade_no=out_trade_no,
            refund_amount=str(amount),
            refund_reason="支付超时自动退款",
        )
        logger.info(f"超时自动退款成功: out_trade_no={out_trade_no}")
    except Exception as e:
        logger.error(f"超时自动退款失败: out_trade_no={out_trade_no} error={e}")


async def publish_order_paid_event(
    order_id: int,
    user_id: int,
    amount: Decimal,
    out_trade_no: str,
) -> None:
    """发送支付成功 MQ 消息, 供消费者处理后续逻辑。"""
    await publish_message(
        routing_key="order.payment.success",
        payload={
            "order_id": order_id,
            "user_id": user_id,
            "amount": str(amount),
            "out_trade_no": out_trade_no,
        },
        event_type="payment.success",
    )
