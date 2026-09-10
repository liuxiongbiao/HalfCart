"""
订单核心服务 — 四态状态机
=========================
严格对齐 PRD 4.2: GATHERING(0)→PURCHASING(1)→DELIVERING(2)→FINISHED(3)
                      ↘ DISBANDED(4)

底座设计 2.3 - t_orders: 完整订单表
底座设计 2.4 - t_order_participants: 参与人记录

规则:
  1. 单向流转 (0→1→2→3), 仅 GATHERING 可解散(0→4)
  2. 现货订单(order_type=2)初始状态为 DELIVERING(2)
  3. 团长自动作为第一个参与人 (role=1)
  4. 状态变更同步发送 RabbitMQ 消息
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.mq_client import MQRoutingKey, publish_message
from app.middleware.exception_handler import AppException
from app.models.order import Order
from app.models.order_participant import OrderParticipant
from app.schemas.common import ErrorCode
from app.utils.constants import OrderStatus, OrderType, ParticipantRole

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────


def _gen_order_no() -> str:
    """生成业务单号: HC + 时间戳(ms) + 6位随机hex → 22字符。"""
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    rand = uuid.uuid4().hex[:6]
    return f"HC{ts}{rand}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _validate_location(lat: Decimal, lng: Decimal) -> None:
    """校验经纬度合法性。"""
    if lat < Decimal("-90") or lat > Decimal("90"):
        raise AppException(code=ErrorCode.PARAM_VALIDATION_ERROR, message=f"纬度非法: {lat}")
    if lng < Decimal("-180") or lng > Decimal("180"):
        raise AppException(code=ErrorCode.PARAM_VALIDATION_ERROR, message=f"经度非法: {lng}")


async def _send_order_sync(
    order_id: int,
    event_type: str,
    payload: dict,
    trace_id: Optional[str] = None,
) -> None:
    """发送 MQ 消息触发 ES 索引同步 (order.status.sync)。MQ 不可用时仅记录警告, 不阻塞业务。"""
    try:
        doc = await _build_es_doc(order_id) if "build_doc" in payload else payload.get("doc")
        await publish_message(
            routing_key=MQRoutingKey.ORDER_STATUS_SYNC,
            payload={"order_id": order_id, "action": "upsert", "doc": doc or payload},
            event_type=event_type,
            trace_id=trace_id,
        )
    except Exception as e:
        logger.exception(f"MQ 消息发送失败 (order_sync {order_id}): {e}")


async def _send_order_notify(
    order_id: int,
    from_status: int,
    to_status: int,
    affected_user_ids: list[int],
    trace_id: Optional[str] = None,
) -> None:
    """发送 MQ 订单状态变更通知 (order.notify)。MQ 不可用时仅记录警告, 不阻塞业务。"""
    try:
        await publish_message(
            routing_key=MQRoutingKey.ORDER_NOTIFY,
            payload={
                "order_id": order_id,
                "from_status": from_status,
                "to_status": to_status,
                "affected_user_ids": affected_user_ids,
            },
            event_type="order.status_changed",
            trace_id=trace_id,
        )
    except Exception as e:
        logger.warning(f"MQ 消息发送失败 (order_notify {order_id}): {e}")


# ──────────────────────────────────────────────
# ES 文档构建 (供同步使用)
# ──────────────────────────────────────────────


async def _build_es_doc(order_id: int) -> dict:
    """构建订单 ES 索引文档 (对齐底座设计 3.2 和 es_client.ORDER_INDEX_MAPPING)。"""
    from app.core.database import async_session_factory
    from sqlalchemy import select
    from app.models.order import Order
    from app.models.user import User

    async with async_session_factory() as session:
        result = await session.execute(select(Order).where(Order.id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            logger.warning(f"_build_es_doc: order_id={order_id} 未找到")
            return {}

        # 读取团长真实信用分
        credit_result = await session.execute(
            select(User.credit_score).where(User.id == order.creator_id)
        )
        credit_score = credit_result.scalar() or 100

    return {
        "order_id": order.id,
        "order_no": order.order_no,
        "creator_id": order.creator_id,
        "status": order.status,
        "order_type": order.order_type,
        "goods_name": order.goods_name or "",
        "goods_desc": order.goods_desc or "",
        "goods_category": order.goods_category or "",
        "price_per_person": float(order.price_per_person),
        "total_people": order.total_people,
        "current_people": order.current_people,
        "location": {"lat": float(order.location_lat), "lon": float(order.location_lng)},
        "location_text": order.meet_location or "",
        "deadline": order.deadline.isoformat() if order.deadline else None,
        "credit_score": credit_score,
        "is_spot": bool(order.is_spot),
        "spot_valid_until": order.spot_valid_until.isoformat() if order.spot_valid_until else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


# ══════════════════════════════════════════════
# 操作 1: 创建普通拼单订单
# PRD 4.2/4.7: GATHERING(0), 团长自动为第一个参与人
# ══════════════════════════════════════════════


async def create_normal_order(
    session: AsyncSession,
    *,
    user_id: int,
    goods_name: str,
    goods_desc: str = "",
    goods_category: str = "",
    purchase_channel: str = "",
    total_people: int = 2,
    price_per_person: Decimal = Decimal("0"),
    meet_location: str = "",
    location_lat: Decimal = Decimal("0"),
    location_lng: Decimal = Decimal("0"),
    deadline: datetime | None = None,
    trace_id: Optional[str] = None,
) -> Order:
    """
    创建普通拼单 — 初始状态 GATHERING(0)。

    业务规则 (PRD 4.2/4.7):
      - 人数 ≥ 2 (含团长)
      - 截单时间 > 当前时间
      - 经纬度合法
      - 团长自动作为第一个参与人 (role=1, pay_status=0)
    """
    if total_people < 2:
        raise AppException(code=ErrorCode.PARAM_VALIDATION_ERROR, message="拼单人数必须≥2 (含团长)")
    _validate_location(location_lat, location_lng)

    now = _now_utc()
    if deadline and deadline <= now:
        raise AppException(code=ErrorCode.ORDER_DEADLINE_EXPIRED, message="截单时间必须晚于当前时间")

    deadline = deadline or (now + timedelta(seconds=settings.ORDER_GATHERING_TIMEOUT_SECONDS))

    order = Order(
        order_no=_gen_order_no(),
        creator_id=user_id,
        status=OrderStatus.GATHERING,
        order_type=OrderType.NORMAL,
        goods_name=goods_name,
        goods_desc=goods_desc,
        goods_category=goods_category,
        purchase_channel=purchase_channel,
        total_people=total_people,
        current_people=1,
        price_per_person=price_per_person,
        total_amount=price_per_person * Decimal(str(total_people - 1)),
        meet_location=meet_location,
        location_lat=location_lat,
        location_lng=location_lng,
        deadline=deadline,
    )
    session.add(order)
    await session.flush()  # 获取 order.id

    # 团长自动加入为参与人 (PRD 4.2)
    participant = OrderParticipant(
        order_id=order.id,
        user_id=user_id,
        role=ParticipantRole.CREATOR,
        pay_status=0,
        pay_amount=Decimal("0"),
    )
    session.add(participant)
    await session.commit()
    await session.refresh(order)

    # 先直接同步到 ES（不依赖 MQ，避免消息丢失导致首页空白）
    try:
        es_doc = await _build_es_doc(order.id)
        from app.core.es_client import upsert_order_doc, ensure_order_index
        await ensure_order_index()
        await upsert_order_doc(order.id, es_doc, refresh=True)
        logger.info(f"ES 直接索引成功: order_id={order.id}")
    except Exception as e:
        logger.exception(f"ES 直接索引失败 (order_id={order.id}): {e}")
    # MQ: ES 同步 + 通知
    await _send_order_sync(order.id, "order.created", {"order_id": order.id, "build_doc": True}, trace_id)

    logger.info(f"普通拼单创建: order_id={order.id} creator={user_id} goods={goods_name}")
    return order


# ══════════════════════════════════════════════
# 操作 2: 创建现货转让订单
# PRD 4.8: 初始状态 DELIVERING(2), is_spot=1
# ══════════════════════════════════════════════


async def create_spot_order(
    session: AsyncSession,
    *,
    user_id: int,
    goods_name: str,
    transfer_price: Decimal,
    meet_location: str = "",
    location_lat: Decimal = Decimal("0"),
    location_lng: Decimal = Decimal("0"),
    receipt_img: str = "",
    original_price: Decimal | None = None,
    goods_category: str = "",
    goods_desc: str = "",
    trace_id: Optional[str] = None,
) -> Order:
    """
    创建现货转让订单 — 初始状态 DELIVERING(2) (PRD 4.8)。

    规则:
      - 转让价格 ≤ 原价 (如有)
      - 现货有效期默认 24h
      - 跳过集资/采购阶段, 直接待交接
    """
    _validate_location(location_lat, location_lng)
    if original_price and transfer_price > original_price:
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message=f"现货转让价格({transfer_price})不得高于原价({original_price})",
        )

    now = _now_utc()
    spot_valid_until = now + timedelta(hours=settings.SPOT_ORDER_VALID_HOURS)

    order = Order(
        order_no=_gen_order_no(),
        creator_id=user_id,
        status=OrderStatus.DELIVERING,  # 现货直接进入待交接 (PRD 4.8)
        order_type=OrderType.SPOT,
        goods_name=goods_name,
        goods_desc=goods_desc,
        goods_category=goods_category,
        total_people=2,       # 1 创建者(卖家) + 1 买家名额
        current_people=1,     # 创建者计入，剩余 1 个购买名额
        price_per_person=transfer_price,
        total_amount=transfer_price,
        meet_location=meet_location,
        location_lat=location_lat,
        location_lng=location_lng,
        deadline=spot_valid_until,
        receipt_img=receipt_img,
        original_price=original_price,
        is_spot=1,
        spot_valid_until=spot_valid_until,
    )
    session.add(order)
    await session.flush()

    participant = OrderParticipant(
        order_id=order.id,
        user_id=user_id,
        role=ParticipantRole.CREATOR,
        pay_status=0,
        pay_amount=Decimal("0"),
    )
    session.add(participant)

    # PRD 4.8: 现货订单直接进入 DELIVERING, 必须在 commit 前生成核销码（保证原子性）
    from app.services.verification_service import create_verification_code as _create_vc
    await _create_vc(session, order.id)

    await session.commit()
    await session.refresh(order)
    logger.info(f"现货订单创建+核销码: order_id={order.id}")

    # 先直接同步到 ES（不依赖 MQ，避免消息丢失导致首页空白）
    try:
        es_doc = await _build_es_doc(order.id)
        from app.core.es_client import upsert_order_doc, ensure_order_index
        await ensure_order_index()
        await upsert_order_doc(order.id, es_doc, refresh=True)
        logger.info(f"ES 直接索引成功(spot): order_id={order.id}")
    except Exception as e:
        logger.exception(f"ES 直接索引失败 (order_id={order.id}): {e}")
    await _send_order_sync(order.id, "order.created", {"order_id": order.id, "build_doc": True}, trace_id)

    logger.info(f"现货订单创建: order_id={order.id} creator={user_id} goods={goods_name}")
    return order


# ══════════════════════════════════════════════
# 操作 3: 订单详情查询
# ══════════════════════════════════════════════


async def get_order_detail(
    session: AsyncSession,
    order_id: int,
    current_user_id: int,
) -> dict:
    """
    订单详情 + 参与人列表 + 当前用户角色。

    Returns:
        { "order": Order, "participants": [...], "user_role": 1|2|None }
    """
    order = await session.get(Order, order_id)
    if not order:
        raise AppException(code=ErrorCode.ORDER_NOT_FOUND, message=f"订单不存在: {order_id}")

    result = await session.execute(
        select(OrderParticipant).where(OrderParticipant.order_id == order_id)
    )
    participants = result.scalars().all()

    user_role = None
    for p in participants:
        if p.user_id == current_user_id:
            user_role = p.role
            break

    return {
        "order": order,
        "participants": list(participants),
        "user_role": user_role,
    }


# ══════════════════════════════════════════════
# 操作 4: 订单列表查询
# ══════════════════════════════════════════════


async def list_my_created_orders(
    session: AsyncSession,
    user_id: int,
    status: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Order], int]:
    """我发起的订单 (分页)。"""
    stmt = select(Order).where(Order.creator_id == user_id, Order.is_deleted == 0)
    if status is not None:
        stmt = stmt.where(Order.status == status)
    stmt = stmt.order_by(Order.created_at.desc())

    from sqlalchemy import func

    count_stmt = select(func.count(Order.id)).where(Order.creator_id == user_id, Order.is_deleted == 0)
    if status is not None:
        count_stmt = count_stmt.where(Order.status == status)

    total_result = await session.execute(count_stmt)
    total = total_result.scalar() or 0

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    orders = result.scalars().all()

    return list(orders), total


async def list_my_joined_orders(
    session: AsyncSession,
    user_id: int,
    status: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Order], int]:
    """我参与的订单 (分页) — 排除本人创建的 (由 _get_participant_order_ids 过滤)。"""
    # 查询用户参与记录
    part_stmt = select(OrderParticipant.order_id).where(
        OrderParticipant.user_id == user_id,
        OrderParticipant.role == ParticipantRole.PARTICIPANT,
    )
    part_result = await session.execute(part_stmt)
    order_ids = [row[0] for row in part_result.all()]

    if not order_ids:
        return [], 0

    stmt = select(Order).where(Order.id.in_(order_ids), Order.is_deleted == 0)
    if status is not None:
        stmt = stmt.where(Order.status == status)
    stmt = stmt.order_by(Order.created_at.desc())

    # count
    from sqlalchemy import func
    count_stmt = select(func.count(Order.id)).where(Order.id.in_(order_ids), Order.is_deleted == 0)
    if status is not None:
        count_stmt = count_stmt.where(Order.status == status)
    total_result = await session.execute(count_stmt)
    total = total_result.scalar() or 0

    stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    return list(result.scalars().all()), total


# ══════════════════════════════════════════════
# 操作 5: 状态流转方法
# ══════════════════════════════════════════════


async def transition_to_purchasing(
    session: AsyncSession,
    order_id: int,
    trace_id: Optional[str] = None,
) -> Order:
    """
    流转: GATHERING(0) → PURCHASING(1) (PRD 4.2)。

    前置条件:
      - 当前状态为 GATHERING
      - current_people >= total_people (人数已满)
    """
    order = await _get_and_lock_order(session, order_id)
    _require_status(order, OrderStatus.GATHERING, "采购")

    if order.current_people < order.total_people:
        raise AppException(
            code=ErrorCode.ORDER_STATUS_TRANSITION_DENIED,
            message=f"人数未满({order.current_people}/{order.total_people}), 不可进入采购",
        )

    old_status = order.status
    order.status = OrderStatus.PURCHASING
    await _commit_and_notify(session, order, old_status, trace_id)

    logger.info(f"订单状态变更: order={order_id} {old_status}→PURCHASING")
    return order


async def transition_to_delivering(
    session: AsyncSession,
    order_id: int,
    operator_id: int,
    trace_id: Optional[str] = None,
) -> Order:
    """
    流转: PURCHASING(1) → DELIVERING(2) (PRD 4.2)。

    前置条件:
      - 当前状态为 PURCHASING
      - 操作人为团长
    """
    order = await _get_and_lock_order(session, order_id)
    _require_status(order, OrderStatus.PURCHASING, "标记已采购")
    _require_creator(order, operator_id)

    old_status = order.status
    order.status = OrderStatus.DELIVERING

    # 生成6位核销码 (PRD 4.5: 仅团长可见, 一次性有效)
    from app.services.verification_service import create_verification_code
    vc = await create_verification_code(session, order_id)

    await _commit_and_notify(session, order, old_status, trace_id)

    logger.info(
        f"订单状态变更: order={order_id} PURCHASING→DELIVERING by {operator_id} "
        f"verify_code={vc.code}"
    )
    return order


async def transition_to_finished(
    session: AsyncSession,
    order_id: int,
    trace_id: Optional[str] = None,
) -> Order:
    """
    流转: DELIVERING(2) → FINISHED(3) (PRD 4.5)。

    前置条件:
      - 当前状态为 DELIVERING
      - 核销码已通过验证 (由调用方校验)
    """
    order = await _get_and_lock_order(session, order_id)
    _require_status(order, OrderStatus.DELIVERING, "核销完成")

    old_status = order.status
    order.status = OrderStatus.FINISHED
    order.finished_at = _now_utc()
    await _commit_and_notify(session, order, old_status, trace_id)

    logger.info(f"订单状态变更: order={order_id} DELIVERING→FINISHED")
    return order


async def disband_order(
    session: AsyncSession,
    order_id: int,
    operator_id: int,
    trace_id: Optional[str] = None,
) -> Order:
    """
    解散订单: GATHERING(0) → DISBANDED(4) (PRD 4.2)。

    前置条件:
      - 当前状态为 GATHERING
      - 操作人为团长 (或超时自动触发, operator_id=0)
    """
    order = await _get_and_lock_order(session, order_id)

    # 允许解散: GATHERING(0) 始终可解散; DELIVERING(2)现货无人购买时可解散
    if order.status == OrderStatus.DELIVERING:
        if not order.is_spot or order.current_people > 1:
            raise AppException(
                code=ErrorCode.ORDER_STATUS_TRANSITION_DENIED,
                message="待交接订单仅现货且无人购买时可取消",
            )
    elif order.status != OrderStatus.GATHERING:
        raise AppException(
            code=ErrorCode.ORDER_STATUS_TRANSITION_DENIED,
            message=f"订单状态 {OrderStatus.label(order.status)} 不允许解散",
        )

    if operator_id != 0:
        _require_creator(order, operator_id)

    old_status = order.status
    order.status = OrderStatus.DISBANDED
    await _commit_and_notify(session, order, old_status, trace_id)

    # ── 批量退款已支付拼友 (PRD 4.6 阶梯退款) ──
    try:
        from app.models.order_participant import OrderParticipant
        from app.models.user import User

        paid_result = await session.execute(
            select(OrderParticipant).where(
                OrderParticipant.order_id == order_id,
                OrderParticipant.pay_status == 1,
            )
        )
        paid_participants = paid_result.scalars().all()

        # 获取团长记录 (用于扣减冻结余额)
        creator = await session.get(User, order.creator_id)

        for p in paid_participants:
            try:
                refund_amount = p.pay_amount
                participant_user = await session.get(User, p.user_id)

                # 扣信用分 (PRD 4.6: GATHERING阶段退出扣1分)
                if participant_user:
                    participant_user.credit_score = max(0, participant_user.credit_score - 1)
                    session.add(participant_user)

                # [FIX P0-6] 资金划转: 团长frozen_balance → 拼友wallet_balance
                if creator and creator.frozen_balance >= refund_amount:
                    creator.frozen_balance -= refund_amount
                    session.add(creator)

                    if participant_user:
                        participant_user.wallet_balance += refund_amount
                        session.add(participant_user)

                    # 生成幂等键 + 写入资金流水
                    from app.utils.idempotent import generate_refund_idempotent_key
                    from app.models.payment_log import PaymentLog
                    from app.utils.constants import PaymentLogType

                    # 出账流水 (团长冻结余额扣减)
                    refund_log = PaymentLog(
                        log_no=f"RFND{order_id:08d}{p.user_id:08d}",
                        user_id=order.creator_id,
                        order_id=order_id,
                        type=PaymentLogType.REFUND,
                        amount=-refund_amount,
                        balance_before=creator.frozen_balance + refund_amount,
                        balance_after=creator.frozen_balance,
                        target_balance_type=2,  # frozen_balance
                        status=1,
                        idempotent_key=generate_refund_idempotent_key(order_id, p.user_id),
                        remark=f"订单解散退款: order={order_id} user={p.user_id}",
                    )
                    session.add(refund_log)

                    # 入账流水 (拼友钱包余额增加)
                    if participant_user:
                        refund_in_log = PaymentLog(
                            log_no=f"RFND{order_id:08d}{p.user_id:08d}I",
                            user_id=p.user_id,
                            order_id=order_id,
                            type=PaymentLogType.REFUND,
                            amount=refund_amount,
                            balance_before=participant_user.wallet_balance - refund_amount,
                            balance_after=participant_user.wallet_balance,
                            target_balance_type=1,  # wallet_balance
                            status=1,
                            idempotent_key=generate_refund_idempotent_key(order_id, p.user_id) + "_in",
                            remark=f"订单解散退款到账: order={order_id}",
                        )
                        session.add(refund_in_log)
                else:
                    logger.error(
                        f"[disband_order] 团长冻结余额不足, 退款需人工介入: "
                        f"order={order_id} user={p.user_id} "
                        f"refund={refund_amount} frozen={creator.frozen_balance if creator else 0}"
                    )

                # 标记退款
                p.pay_status = 2  # 已退款
                p.refund_amount = refund_amount
                p.refund_ratio = 1.0
                p.refund_reason = f"订单解散 (order_id={order_id})"
                session.add(p)

            except Exception as refund_err:
                logger.error(
                    f"[disband_order] 退款失败需人工介入: order={order_id} "
                    f"user={p.user_id} error={refund_err}"
                )

        if paid_participants:
            await session.commit()
            logger.warning(
                f"[disband_order] 订单解散完成: order={order_id} "
                f"已处理 {len(paid_participants)} 笔退款, "
                f"支付宝侧退款需人工操作!"
            )
    except Exception as e:
        logger.error(
            f"[disband_order] 批量退款异常需人工介入: order={order_id} error={e}"
        )

    logger.info(f"订单已解散: order={order_id} by {operator_id}")
    return order


# ══════════════════════════════════════════════
# 操作 6: 删除订单 (软删除/解散)
# ══════════════════════════════════════════════


async def delete_order(
    session: AsyncSession,
    order_id: int,
    user_id: int,
    trace_id: Optional[str] = None,
) -> dict:
    """
    删除订单 (PRD 4.2 补充: 订单删除)。

    业务规则:
      - 仅订单创建者可删除
      - DELIVERING(2) 待交接 → 拒绝删除, 需先完成交接
      - GATHERING(0) / PURCHASING(1) → 解散订单 status→DISBANDED(4)
      - FINISHED(3) / DISBANDED(4) → 软删除 is_deleted=1

    Returns: { "action": "disbanded"|"soft_deleted", "message": str }
    """
    from app.schemas.common import ErrorCode as EC

    order = await _get_and_lock_order(session, order_id)
    _require_creator(order, user_id)

    # DELIVERING 待交接: 不可删除
    if order.status == OrderStatus.DELIVERING:
        raise AppException(
            code=EC.ORDER_STATUS_TRANSITION_DENIED,
            message="待交接订单不可删除，请先完成交接后再操作",
        )

    # GATHERING / PURCHASING: 解散订单
    if order.status in (OrderStatus.GATHERING, OrderStatus.PURCHASING):
        old_status = order.status
        order.status = OrderStatus.DISBANDED
        await _commit_and_notify(session, order, old_status, trace_id)

        # 批量退款 (同 disband_order)
        try:
            from app.models.order_participant import OrderParticipant
            from app.models.user import User

            paid_result = await session.execute(
                select(OrderParticipant).where(
                    OrderParticipant.order_id == order_id,
                    OrderParticipant.pay_status == 1,
                )
            )
            paid_participants = paid_result.scalars().all()

            creator = await session.get(User, order.creator_id)

            for p in paid_participants:
                try:
                    refund_amount = p.pay_amount
                    participant_user = await session.get(User, p.user_id)

                    if participant_user:
                        participant_user.credit_score = max(0, participant_user.credit_score - 1)
                        session.add(participant_user)

                    if creator and creator.frozen_balance >= refund_amount:
                        creator.frozen_balance -= refund_amount
                        session.add(creator)

                        if participant_user:
                            participant_user.wallet_balance += refund_amount
                            session.add(participant_user)

                        from app.utils.idempotent import generate_refund_idempotent_key
                        from app.models.payment_log import PaymentLog
                        from app.utils.constants import PaymentLogType

                        refund_log = PaymentLog(
                            log_no=f"DLRF{order_id:08d}{p.user_id:08d}",
                            user_id=order.creator_id,
                            order_id=order_id,
                            type=PaymentLogType.REFUND,
                            amount=-refund_amount,
                            balance_before=creator.frozen_balance + refund_amount,
                            balance_after=creator.frozen_balance,
                            target_balance_type=2,
                            status=1,
                            idempotent_key=generate_refund_idempotent_key(order_id, p.user_id) + "_del",
                            remark=f"订单删除退款: order={order_id} user={p.user_id}",
                        )
                        session.add(refund_log)

                        if participant_user:
                            refund_in_log = PaymentLog(
                                log_no=f"DLRF{order_id:08d}{p.user_id:08d}I",
                                user_id=p.user_id,
                                order_id=order_id,
                                type=PaymentLogType.REFUND,
                                amount=refund_amount,
                                balance_before=participant_user.wallet_balance - refund_amount,
                                balance_after=participant_user.wallet_balance,
                                target_balance_type=1,
                                status=1,
                                idempotent_key=generate_refund_idempotent_key(order_id, p.user_id) + "_del_in",
                                remark=f"订单删除退款到账: order={order_id}",
                            )
                            session.add(refund_in_log)

                    p.pay_status = 2
                    p.refund_amount = refund_amount
                    p.refund_ratio = 1.0
                    p.refund_reason = f"订单删除 (order_id={order_id})"
                    session.add(p)

                except Exception as refund_err:
                    logger.error(f"[delete_order] 退款失败: order={order_id} user={p.user_id} error={refund_err}")

            if paid_participants:
                await session.commit()
        except Exception as e:
            logger.error(f"[delete_order] 批量退款异常: order={order_id} error={e}")

        logger.info(f"订单删除(解散): order={order_id} by {user_id}")
        return {"action": "disbanded", "message": "订单已解散"}

    # FINISHED / DISBANDED: 软删除
    order.is_deleted = 1
    await session.commit()
    await session.refresh(order)

    # ES 同步: 从索引中移除
    try:
        await _send_order_sync(order.id, "order.deleted", {"order_id": order.id, "action": "delete"}, trace_id)
    except Exception:
        pass

    logger.info(f"订单删除(软删): order={order_id} by {user_id}")
    return {"action": "soft_deleted", "message": "订单已删除"}
# ══════════════════════════════════════════════


async def _get_and_lock_order(session: AsyncSession, order_id: int) -> Order:
    """SELECT ... FOR UPDATE 行级锁读取订单。"""
    result = await session.execute(
        select(Order).where(Order.id == order_id).with_for_update()
    )
    order = result.scalar_one_or_none()
    if not order:
        raise AppException(code=ErrorCode.ORDER_NOT_FOUND, message=f"订单不存在: {order_id}")
    return order


def _require_status(order: Order, expected: int, action: str) -> None:
    """校验订单当前状态。"""
    if order.status != expected:
        raise AppException(
            code=ErrorCode.ORDER_STATUS_TRANSITION_DENIED,
            message=(
                f"订单状态 {OrderStatus.label(order.status)} 不允许{action}"
                f"，需要状态为 {OrderStatus.label(expected)}"
            ),
        )


def _require_creator(order: Order, user_id: int) -> None:
    """校验操作人是否为团长。"""
    if order.creator_id != user_id:
        raise AppException(
            code=ErrorCode.ORDER_CREATOR_ONLY,
            message=f"仅团长可执行此操作 (团长ID={order.creator_id})",
        )


async def _commit_and_notify(
    session: AsyncSession,
    order: Order,
    old_status: int,
    trace_id: Optional[str] = None,
) -> None:
    """提交事务 + 发送 MQ 通知。"""
    await session.commit()
    await session.refresh(order)

    # 查询参与人列表
    result = await session.execute(
        select(OrderParticipant.user_id).where(OrderParticipant.order_id == order.id)
    )
    affected = [row[0] for row in result.all()]

    await _send_order_notify(order.id, old_status, order.status, affected, trace_id)
    # 使用 _build_es_doc 统一构建 ES 文档，避免与直接创建路径的字段不一致
    try:
        es_doc = await _build_es_doc(order.id)
        await _send_order_sync(
            order.id,
            "order.status_changed",
            {"order_id": order.id, "doc": es_doc},
            trace_id,
        )
    except Exception as e:
        logger.exception(f"状态变更后 ES 同步失败: order_id={order.id} error={e}")


def is_order_expired(order: Order) -> bool:
    """检查订单截单时间是否已过 (兼容MySQL存储的无时区datetime)。"""
    if order.deadline:
        # MySQL 存储的 datetime 无时区, 统一作为 UTC 比对
        dl = order.deadline
        if dl.tzinfo is None:
            dl = dl.replace(tzinfo=timezone.utc)
        return _now_utc() > dl
    return False


def is_creator(order: Order, user_id: int) -> bool:
    """判断用户是否为订单团长。"""
    return order.creator_id == user_id


def validate_order_status(order: Order, expected: int) -> bool:
    """校验订单状态是否为期望值。"""
    return order.status == expected
