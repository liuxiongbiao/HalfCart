"""
纠纷处理与AI智能裁判服务
========================
PRD 4.9: 纠纷提交 → MQ异步 → Dify多模态裁判 → 自动资金划转。

约束:
  - AI裁判 → dify_service.judge_dispute()
  - 资金操作 → balance_service
  - 异步解耦 → 提交发MQ, ai_task_consumer回调execute_ai_judge
"""

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.mq_client import publish_ai_dispute_task
from app.middleware.exception_handler import AppException
from app.models.dispute import Dispute
from app.models.order import Order
from app.models.order_participant import OrderParticipant
from app.schemas.common import ErrorCode
from app.services import balance_service
from app.services.dify_service import judge_dispute
from app.utils.constants import DisputeStatus, DisputeType, OrderStatus, PaymentLogType

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = settings.DISPUTE_JUDGE_CONFIDENCE_THRESHOLD


# ══════════════════════════════════════════════
# 用户发起纠纷
# ══════════════════════════════════════════════


async def initiate_dispute(
    session: AsyncSession,
    user_id: int,
    order_id: int,
    dispute_type: int,
    description: str,
    evidence_images: list[str],
    *,
    trace_id: Optional[str] = None,
) -> Dispute:
    """
    发起纠纷 (PRD 4.9)。

    校验 → 创建记录 → 发送 MQ AI 任务 → 返回纠纷ID。
    """
    # 校验订单
    order = await session.get(Order, order_id)
    if not order:
        raise AppException(code=ErrorCode.ORDER_NOT_FOUND, message="订单不存在")
    if order.status not in (OrderStatus.DELIVERING, OrderStatus.FINISHED):
        raise AppException(
            code=ErrorCode.DISPUTE_NOT_ALLOWED,
            message="仅待交接/已完成订单可发起纠纷",
        )
    # 校验参与人
    part_result = await session.execute(
        select(OrderParticipant).where(
            OrderParticipant.order_id == order_id,
            OrderParticipant.user_id == user_id,
        )
    )
    if not part_result.scalar_one_or_none():
        raise AppException(code=ErrorCode.ORDER_NOT_MEMBER, message="仅订单参与人可发起纠纷")

    # ── PRD 4.9: 涉及金额≥500元 → 不启用自动裁判, 直接流转人工 ──
    DISPUTE_AUTO_MAX_AMOUNT = Decimal(str(settings.DISPUTE_AUTO_JUDGE_MAX_AMOUNT))
    if order.total_amount >= DISPUTE_AUTO_MAX_AMOUNT:
        logger.info(
            f"纠纷金额≥500元, 进入人工审核: order={order_id} "
            f"amount={order.total_amount}"
        )
        # 创建纠纷记录但标记为人工审核
        dispute = Dispute(
            order_id=order_id,
            applicant_id=user_id,
            dispute_type=dispute_type,
            description=description,
            evidence_images=evidence_images,
            status=DisputeStatus.PENDING_MANUAL,
        )
        session.add(dispute)
        order.dispute_status = DisputeStatus.PENDING_MANUAL
        session.add(order)
        await session.commit()
        await session.refresh(dispute)
        return dispute

    # 幂等: 同一用户同一订单的未关闭纠纷直接返回 (PRD 4.9)
    existing = await session.execute(
        select(Dispute).where(
            Dispute.order_id == order_id,
            Dispute.applicant_id == user_id,
            Dispute.status != DisputeStatus.CLOSED,
        )
    )
    dup = existing.scalar_one_or_none()
    if dup:
        logger.info(f"幂等: 纠纷已存在 dispute={dup.id}")
        return dup

    # 其他用户已发起纠纷 → 拒绝
    if order.dispute_status != 0:
        raise AppException(code=ErrorCode.DISPUTE_DUPLICATE, message="该订单已有处理中的纠纷")

    if not evidence_images:
        raise AppException(code=ErrorCode.PARAM_VALIDATION_ERROR, message="至少上传1张凭证图片")

    # 创建纠纷记录
    dispute = Dispute(
        order_id=order_id,
        applicant_id=user_id,
        dispute_type=dispute_type,
        description=description,
        evidence_images=json.dumps(evidence_images, ensure_ascii=False),
        status=DisputeStatus.PENDING,
    )
    session.add(dispute)
    order.dispute_status = 1  # 订单标记纠纷中
    session.add(order)
    await session.commit()
    await session.refresh(dispute)

    # 发送 MQ AI 任务
    await publish_ai_dispute_task(
        dispute_id=dispute.id,
        order_id=order_id,
        applicant_id=user_id,
        description=description,
        image_urls=evidence_images,
        trace_id=trace_id,
    )

    logger.info(f"纠纷已提交: dispute={dispute.id} order={order_id} applicant={user_id}")
    return dispute


# ══════════════════════════════════════════════
# 执行 AI 裁判 (供 ai_task_consumer 调用)
# ══════════════════════════════════════════════


async def execute_ai_judge(
    session: AsyncSession,
    dispute_id: int,
    *,
    trace_id: Optional[str] = None,
) -> Dispute:
    """
    执行 AI 裁判 (PRD 4.9 — 由 MQ消费者触发)。

    流程:
      1. 查询纠纷 + 调用 Dify
      2. 置信度≥80% → 自动执行资金划转 → 状态=已完成
      3. 置信度<80% → 状态=待人工处理 (保留AI建议)
    """
    dispute = await session.get(Dispute, dispute_id)
    if not dispute:
        raise AppException(code=ErrorCode.DISPUTE_NOT_FOUND, message="纠纷不存在")
    if dispute.status != DisputeStatus.PENDING:
        logger.info(f"幂等: 纠纷 {dispute_id} 已处理, 跳过AI裁判")
        return dispute

    # 解析凭证图片
    try:
        images = json.loads(dispute.evidence_images) if isinstance(dispute.evidence_images, str) else dispute.evidence_images
    except json.JSONDecodeError:
        images = [dispute.evidence_images]

    # 调用 Dify 裁判工作流
    result = await judge_dispute(
        description=dispute.description,
        image_urls=images,
        trace_id=trace_id,
    )

    # 写入 AI 结果
    dispute.ai_responsible_party = result.responsible_party
    dispute.ai_creator_ratio = result.creator_ratio
    dispute.ai_participant_ratio = result.participant_ratio
    dispute.ai_confidence = result.confidence
    dispute.ai_reason = result.reason

    now = datetime.now(timezone.utc)

    if result.confidence >= CONFIDENCE_THRESHOLD:
        # 自动执行资金划转
        order = await session.get(Order, dispute.order_id)
        await _execute_settlement(session, dispute, order, result)
        dispute.status = DisputeStatus.RESOLVED
        dispute.resolved_at = now
        if order:
            order.dispute_status = 2  # 已结案
            session.add(order)
    else:
        # 待人工处理
        dispute.status = DisputeStatus.PENDING_MANUAL
        logger.warning(f"纠纷 {dispute_id} AI置信度{result.confidence}<{CONFIDENCE_THRESHOLD}, 转人工")

    session.add(dispute)
    await session.commit()
    await session.refresh(dispute)

    logger.info(f"AI裁判完成: dispute={dispute_id} status={dispute.status} party={result.responsible_party}")
    return dispute


async def _execute_settlement(session, dispute: Dispute, order: Order, result) -> None:
    """
    根据 AI 判责比例执行资金划转 (PRD 4.9)。

    - participant全责: 拼友frozen → 团长wallet
    - creator全责: 团长frozen → 拼友wallet
    - shared: 按比例分别划转
    """
    ts = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    base_key = f"dispute:{dispute.id}:{ts}"

    part_result = await session.execute(
        select(OrderParticipant).where(OrderParticipant.order_id == dispute.order_id)
    )
    participants = part_result.scalars().all()
    paid_amounts = {p.user_id: p.pay_amount for p in participants}
    creator_id = order.creator_id
    applicant_id = dispute.applicant_id
    total_amount = sum(paid_amounts.values())

    # 金额: Decimal × float → Decimal (必须转换, Decimal*float会TypeError)
    c_ratio = Decimal(str(result.creator_ratio))
    p_ratio = Decimal(str(result.participant_ratio))
    creator_responsibility = (total_amount * c_ratio).quantize(Decimal("0.01"))
    participant_responsibility = (total_amount * p_ratio).quantize(Decimal("0.01"))

    if result.responsible_party == "creator":
        # 团长全责: 从团长frozen退还给拼友
        refund = min(paid_amounts.get(applicant_id, total_amount), creator_responsibility)
        await balance_service.wallet_balance_increase(
            session=session, user_id=applicant_id, order_id=dispute.order_id,
            amount=refund, log_type=PaymentLogType.DISPUTE_REFUND,
            idempotent_key=f"{base_key}:creator_refund",
            remark=f"纠纷{dispute.id} 团长全责赔付 {refund}",
        )
    elif result.responsible_party == "participant":
        # 拼友全责: 违约金给团长
        penalty = min(paid_amounts.get(applicant_id, total_amount), participant_responsibility)
        await balance_service.wallet_balance_increase(
            session=session, user_id=creator_id, order_id=dispute.order_id,
            amount=penalty, log_type=PaymentLogType.DISPUTE_REFUND,
            idempotent_key=f"{base_key}:penalty",
            remark=f"纠纷{dispute.id} 拼友全责赔付团长 {penalty}",
        )
    else:
        # 双方分担
        if creator_responsibility > 0:
            await balance_service.wallet_balance_increase(
                session=session, user_id=applicant_id, order_id=dispute.order_id,
                amount=creator_responsibility, log_type=PaymentLogType.DISPUTE_REFUND,
                idempotent_key=f"{base_key}:shared_creator",
                remark=f"纠纷{dispute.id} 团长承担 {creator_responsibility}",
            )
        if participant_responsibility > 0:
            await balance_service.wallet_balance_increase(
                session=session, user_id=creator_id, order_id=dispute.order_id,
                amount=participant_responsibility, log_type=PaymentLogType.DISPUTE_REFUND,
                idempotent_key=f"{base_key}:shared_participant",
                remark=f"纠纷{dispute.id} 拼友承担 {participant_responsibility}",
            )


# ══════════════════════════════════════════════
# 查询
# ══════════════════════════════════════════════


async def get_dispute_detail(
    session: AsyncSession, dispute_id: int, user_id: int
) -> Dispute:
    dispute = await session.get(Dispute, dispute_id)
    if not dispute:
        raise AppException(code=ErrorCode.DISPUTE_NOT_FOUND, message="纠纷不存在")
    # 权限: 仅订单参与人可查看
    part_result = await session.execute(
        select(OrderParticipant).where(
            OrderParticipant.order_id == dispute.order_id,
            OrderParticipant.user_id == user_id,
        )
    )
    if not part_result.scalar_one_or_none():
        raise AppException(code=ErrorCode.DISPUTE_PERMISSION_DENIED, message="无权查看该纠纷")
    return dispute


async def list_my_disputes(
    session: AsyncSession,
    user_id: int,
    status: Optional[int] = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[Dispute], int]:
    stmt = select(Dispute).where(Dispute.applicant_id == user_id)
    cnt = select(func.count(Dispute.id)).where(Dispute.applicant_id == user_id)
    if status is not None:
        stmt = stmt.where(Dispute.status == status)
        cnt = cnt.where(Dispute.status == status)
    total_result = await session.execute(cnt)
    total = total_result.scalar() or 0
    stmt = stmt.order_by(Dispute.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    result = await session.execute(stmt)
    return list(result.scalars().all()), total
