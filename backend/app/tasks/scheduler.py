"""
定时任务调度器 — APScheduler
=============================
PRD 3.1/4.2/4.5/4.8: 全量定时任务统一管理。

任务清单:
  1. 增量对账 (每小时): 扫描近24h活跃订单, 比对流水/订单/余额一致性
  2. 全量对账 (每天02:00): 全量活跃用户与进行中订单一致性校验
  3. 阅后即焚 (每天03:00): FINISHED超24h订单, 物理删除聊天记录
  4. 超时解散 (每60秒): GATHERING截止时间到→自动解散全额退款
  5. 现货下架 (每60秒): 现货超24h未成交→自动下架
  6. 待交接超时告警 (每小时): DELIVERING超7天→触发人工介入告警
"""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.core.database import async_session_factory
from app.utils.constants import PaymentLogType
from app.core.mq_client import (
    publish_chat_cleanup_trigger,
    publish_message,
    publish_reconcile_trigger,
)

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


# ══════════════════════════════════════════════
# 任务 1: 增量对账 (每小时)
# ══════════════════════════════════════════════


async def reconcile_task() -> None:
    """增量对账: 扫描近24h活跃订单, 三方比对自动补偿。"""
    logger.info("[Scheduler] 增量对账任务触发")
    try:
        from app.services.reconciliation_service import run_incremental_reconciliation

        async with async_session_factory() as session:
            record = await run_incremental_reconciliation(session)
            logger.info(
                f"[Scheduler] 增量对账完成: recon_id={record.id} "
                f"anomalies={record.anomalies_total} auto_fixed={record.auto_fixed}"
            )
    except Exception as e:
        logger.error(f"[Scheduler] 增量对账异常: {e}", exc_info=True)


async def full_reconciliation_task() -> None:
    """全量对账: 每天凌晨全量校验。"""
    logger.info("[Scheduler] 全量对账任务触发")
    try:
        from app.services.reconciliation_service import run_full_reconciliation

        async with async_session_factory() as session:
            record = await run_full_reconciliation(session)
            logger.info(
                f"[Scheduler] 全量对账完成: recon_id={record.id} "
                f"anomalies={record.anomalies_total}"
            )
    except Exception as e:
        logger.error(f"[Scheduler] 全量对账异常: {e}", exc_info=True)


# ══════════════════════════════════════════════
# 任务 2: 阅后即焚 (每天03:00)
# ══════════════════════════════════════════════


async def chat_cleanup_task() -> None:
    """阅后即焚: FINISHED 24h后物理删除聊天记录。"""
    logger.info("[Scheduler] 阅后即焚清理任务触发")
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.CHAT_CLEANUP_HOURS)
        success = await publish_chat_cleanup_trigger(order_id=0, finished_at=cutoff.isoformat())
        if success:
            logger.info("[Scheduler] 阅后即焚清理消息已发布")
        else:
            logger.error("[Scheduler] 清理消息发布失败 (MQ不可用)")
    except Exception as e:
        logger.error(f"[Scheduler] 阅后即焚异常: {e}", exc_info=True)


# ══════════════════════════════════════════════
# 任务 3: 超时订单解散 (每60秒)
# ══════════════════════════════════════════════


async def auto_disband_task() -> None:
    """
    超时/未满员 GATHERING 订单自动解散 (PRD 4.2)。

    逻辑:
      1. 查询 status=0 AND deadline < now 的订单
      2. 逐个执行解散: 全额退款已支付拼友 → 信用分各-1 → 订单→DISBANDED
      3. 通过 MQ 发布解散事件通知
    """
    from sqlalchemy import select, update
    from app.models.order import Order
    from app.models.order_participant import OrderParticipant
    from app.utils.constants import OrderStatus, ParticipantPayStatus

    try:
        now = datetime.now(timezone.utc)
        async with async_session_factory() as session:
            result = await session.execute(
                select(Order).where(
                    Order.status == OrderStatus.GATHERING,
                    Order.deadline < now,
                )
            )
            expired_orders = result.scalars().all()

            disband_count = 0
            for order in expired_orders:
                # 查询已支付的参与者
                participants_result = await session.execute(
                    select(OrderParticipant).where(
                        OrderParticipant.order_id == order.id,
                        OrderParticipant.pay_status == ParticipantPayStatus.PAID,
                    )
                )
                paid = participants_result.scalars().all()

                # 全额退款每个已支付拼友 + 扣信用分
                for p in paid:
                    # 解冻团长冻结余额
                    from app.services import balance_service
                    from app.utils.idempotent import BusinessScene, generate_idempotent_key

                    idem_key = generate_idempotent_key(
                        BusinessScene.REFUND_UNFREEZE,
                        p.user_id,
                        order.id,
                        f"disband:{p.id}",
                    )
                    try:
                        await balance_service.freeze_balance_decrease(
                            session=session, user_id=order.creator_id,
                            amount=p.pay_amount, order_id=order.id, idempotent_key=idem_key,
                        )
                        # [FIX] 退款转回拼友钱包 (PRD 4.2: 解散全额退款)
                        await balance_service.wallet_balance_increase(
                            session=session, user_id=p.user_id,
                            amount=p.pay_amount, order_id=order.id,
                            idempotent_key=f"{idem_key}:wallet",
                            log_type=PaymentLogType.REFUND_UNFREEZE,
                            remark=f"订单 {order.id} 超时解散, 退款到账",
                        )
                    except Exception as e:
                        logger.warning(f"[Scheduler] 解散退款失败: participant={p.id} err={e}")
                    from app.models.user import User
                    user = await session.get(User, p.user_id)
                    if user:
                        user.credit_score = max(0, user.credit_score - 1)
                        session.add(user)

                order.status = OrderStatus.DISBANDED
                session.add(order)
                disband_count += 1

            if disband_count > 0:
                await session.commit()
                logger.info(f"[Scheduler] 已解散 {disband_count} 个超时订单")
    except Exception as e:
        logger.error(f"[Scheduler] 超时解散异常: {e}", exc_info=True)


# ══════════════════════════════════════════════
# 任务 4: 现货下架 (每60秒)
# ══════════════════════════════════════════════


async def spot_expiry_task() -> None:
    """
    现货到期自动下架 (PRD 4.8)。

    逻辑: DELIVERING 现货订单超过 spot_valid_until → 状态→DISBANDED。
    """
    from sqlalchemy import select
    from app.models.order import Order
    from app.utils.constants import OrderStatus

    try:
        now = datetime.now(timezone.utc)
        async with async_session_factory() as session:
            result = await session.execute(
                select(Order).where(
                    Order.status == OrderStatus.DELIVERING,
                    Order.is_spot == 1,
                    Order.spot_valid_until < now,
                )
            )
            expired = result.scalars().all()

            for order in expired:
                order.status = OrderStatus.DISBANDED
                session.add(order)

            if expired:
                await session.commit()
                logger.info(f"[Scheduler] 已下架 {len(expired)} 个过期现货订单")
    except Exception as e:
        logger.error(f"[Scheduler] 现货下架异常: {e}", exc_info=True)


# ══════════════════════════════════════════════
# 任务 5: 待交接超时告警 (每小时)
# ══════════════════════════════════════════════


async def delivering_timeout_alert_task() -> None:
    """
    DELIVERING 超7天未核销 → 触发人工介入告警 (PRD 4.2)。

    通过 MQ 发布告警事件, 运营后台可查看。
    """
    from sqlalchemy import select
    from app.models.order import Order
    from app.utils.constants import OrderStatus

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        async with async_session_factory() as session:
            result = await session.execute(
                select(Order).where(
                    Order.status == OrderStatus.DELIVERING,
                    Order.updated_at < cutoff,
                )
            )
            stale = result.scalars().all()

            if stale:
                for order in stale:
                    try:
                        await publish_message(
                            routing_key="order.alert",
                            payload={
                                "order_id": order.id,
                                "order_no": order.order_no,
                                "alert_type": "delivering_timeout",
                                "days_stale": 7,
                            },
                            event_type="alert.delivering_timeout",
                        )
                    except Exception:
                        pass

                logger.warning(
                    f"[Scheduler] DELIVERING超时告警: {len(stale)} 个订单超7天未核销"
                )
    except Exception as e:
        logger.error(f"[Scheduler] DELIVERING超时告警异常: {e}", exc_info=True)


# ══════════════════════════════════════════════
# 调度器启动/关闭
# ══════════════════════════════════════════════


def start_scheduler() -> None:
    """启动 APScheduler 并注册全部定时任务。"""
    logger.info("[Scheduler] 初始化定时任务调度器...")

    scheduler.add_job(
        reconcile_task,
        trigger=IntervalTrigger(seconds=settings.RECONCILE_CRON_INTERVAL_SECONDS),
        id="reconcile", name="增量对账", replace_existing=True,
    )
    scheduler.add_job(
        full_reconciliation_task,
        trigger=CronTrigger(hour=2, minute=0),
        id="full_reconcile", name="全量对账", replace_existing=True,
    )
    scheduler.add_job(
        chat_cleanup_task,
        trigger=CronTrigger(hour=3, minute=0),
        id="chat_cleanup", name="阅后即焚清理", replace_existing=True,
    )
    scheduler.add_job(
        auto_disband_task,
        trigger=IntervalTrigger(seconds=60),
        id="auto_disband", name="超时订单解散", replace_existing=True,
    )
    scheduler.add_job(
        spot_expiry_task,
        trigger=IntervalTrigger(seconds=60),
        id="spot_expiry", name="现货到期下架", replace_existing=True,
    )
    scheduler.add_job(
        delivering_timeout_alert_task,
        trigger=IntervalTrigger(seconds=3600),
        id="delivering_alert", name="待交接超时告警(7天)", replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "[Scheduler] 调度器已启动: 对账=每小时 | 全量对账=02:00 | "
        "清理=03:00 | 解散+现货下架=每60秒 | 待交接告警=每小时"
    )


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("[Scheduler] 调度器已关闭")
