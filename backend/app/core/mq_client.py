"""
RabbitMQ 异步生产者封装
=======================
对应PRD:
- 4.7 AI语音发单: ai.asr.order
- 4.8 OCR现货转让: ai.ocr.spot
- 4.9 AI纠纷裁判: ai.dispute.judge
- 4.2/4.4 订单通知: order.notify
- 4.3 LBS同步: order.status.sync
- 4.5 增量对账: order.reconcile
- 3.1 阅后即焚: order.chat.cleanup

对应底座设计 4.1-4.4:
  - Topic交换机: halfcart.exchange
  - 7个队列 + 5个死信队列
  - 统一消息格式 (event_id + trace_id)
"""

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import aio_pika
from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractExchange

from app.config import settings

logger = logging.getLogger(__name__)

# 全局连接
_mq_connection: Optional[AbstractConnection] = None
_mq_channel: Optional[AbstractChannel] = None
_mq_exchange: Optional[AbstractExchange] = None

# ──────────────────────────────────────────────
# 队列 Routing Key 常量 (底座设计 4.2)
# ──────────────────────────────────────────────


class MQRoutingKey:
    """RabbitMQ Routing Key 常量，对应底座设计 4.2 队列规划。"""

    AI_ASR_ORDER = "ai.asr.order"           # AI语音发单 (PRD 4.7)
    AI_OCR_SPOT = "ai.ocr.spot"             # OCR现货转让 (PRD 4.8)
    AI_DISPUTE_JUDGE = "ai.dispute.judge"   # AI纠纷裁判 (PRD 4.9)
    ORDER_NOTIFY = "order.notify"           # 订单状态通知 (PRD 4.2/4.4)
    ORDER_STATUS_SYNC = "order.status.sync" # ES索引同步 (PRD 4.3)
    ORDER_RECONCILE = "order.reconcile"     # 增量对账 (PRD 4.5)
    ORDER_CHAT_CLEANUP = "order.chat.cleanup"  # 阅后即焚 (PRD 3.1)
    PAYMENT_SUCCESS = "order.payment.success"  # 支付宝支付成功 (PRD V3.2)

    # 所有 routing key 列表（用于声明队列时遍历）
    ALL_KEYS = [
        AI_ASR_ORDER,
        AI_OCR_SPOT,
        AI_DISPUTE_JUDGE,
        ORDER_NOTIFY,
        ORDER_STATUS_SYNC,
        ORDER_RECONCILE,
        ORDER_CHAT_CLEANUP,
        PAYMENT_SUCCESS,
    ]


# ──────────────────────────────────────────────
# 生命周期管理
# ──────────────────────────────────────────────

async def init_mq() -> None:
    """
    初始化 RabbitMQ 连接、Channel、Topic交换机。
    由 main.py lifespan 启动阶段调用。

    底座设计 4.1: halfcart.exchange (topic类型, 持久化)
    """
    global _mq_connection, _mq_channel, _mq_exchange

    _mq_connection = await aio_pika.connect_robust(
        settings.RABBITMQ_URL,
        reconnect_interval=settings.MQ_RECONNECT_INTERVAL,
    )

    _mq_channel = await _mq_connection.channel()
    # 设置 Qos: 每次最多预取1条消息
    await _mq_channel.set_qos(prefetch_count=1)

    # 声明 Topic 交换机（持久化）
    _mq_exchange = await _mq_channel.declare_exchange(
        name=settings.MQ_EXCHANGE,
        type=aio_pika.ExchangeType.TOPIC,
        durable=True,  # 交换机持久化
    )

    # 声明所有业务队列 + 死信队列 (底座设计 4.2 + 4.3)
    # 死信队列参数
    dlx_exchange_name = f"{settings.MQ_EXCHANGE}.dlx"

    # 声明死信交换机
    dlx_exchange = await _mq_channel.declare_exchange(
        name=dlx_exchange_name,
        type=aio_pika.ExchangeType.TOPIC,
        durable=True,
    )

    for routing_key in MQRoutingKey.ALL_KEYS:
        dlq_name = f"dlq.{routing_key}"

        # 声明死信队列
        dlq = await _mq_channel.declare_queue(
            name=dlq_name,
            durable=True,
        )
        await dlq.bind(dlx_exchange, routing_key=dlq_name)

        # 声明业务队列，绑定死信交换机
        queue = await _mq_channel.declare_queue(
            name=routing_key,
            durable=True,
            arguments={
                "x-dead-letter-exchange": dlx_exchange_name,
                "x-dead-letter-routing-key": dlq_name,
                "x-message-ttl": 300000,  # 消息TTL 5分钟
            },
        )
        await queue.bind(_mq_exchange, routing_key=routing_key)

    logger.info(
        f"RabbitMQ 初始化完成: exchange={settings.MQ_EXCHANGE}, "
        f"queues={len(MQRoutingKey.ALL_KEYS)}"
    )


async def close_mq() -> None:
    """关闭 RabbitMQ 连接。"""
    global _mq_connection, _mq_channel, _mq_exchange
    if _mq_connection:
        await _mq_connection.close()
        _mq_connection = None
        _mq_channel = None
        _mq_exchange = None


def get_exchange() -> AbstractExchange:
    """获取 Topic 交换机实例。"""
    if _mq_exchange is None:
        raise RuntimeError("RabbitMQ 未初始化，请先调用 init_mq()")
    return _mq_exchange


# ──────────────────────────────────────────────
# 消息发布
# ──────────────────────────────────────────────

async def publish_message(
    routing_key: str,
    payload: dict[str, Any],
    event_type: Optional[str] = None,
    trace_id: Optional[str] = None,
    message_id: Optional[str] = None,
) -> bool:
    """
    发布消息到指定队列。

    底座设计 4.4 统一消息格式:
        {
            "event_id": "uuid",      # 消息唯一ID (幂等)
            "event_type": "...",     # 事件类型
            "timestamp": "ISO8601",  # 时间戳
            "source": "halfcart-backend",
            "payload": { ... },
            "trace_id": "uuid"       # 全链路追踪
        }

    Args:
        routing_key: 路由键 (使用 MQRoutingKey 常量)
        payload: 业务荷载数据
        event_type: 事件类型标签 (可选)
        trace_id: 全链路追踪ID (可选)
        message_id: 幂等消息ID (可选，默认自动生成UUID)

    Returns:
        投递成功返回 True，否则 False
    """
    exchange = get_exchange()

    message_body = {
        "event_id": message_id or str(uuid.uuid4()),
        "event_type": event_type or routing_key,
        "timestamp": _now_iso(),
        "source": f"{settings.APP_NAME}-backend",
        "payload": payload,
        "trace_id": trace_id or str(uuid.uuid4()),
    }

    try:
        message = Message(
            body=json.dumps(message_body, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,  # 消息持久化
            message_id=message_body["event_id"],
            timestamp=_now_dt(),
        )

        await exchange.publish(
            message=message,
            routing_key=routing_key,
        )

        logger.debug(
            f"MQ消息已投递: routing_key={routing_key} "
            f"event_id={message_body['event_id']}"
        )
        return True
    except Exception as e:
        logger.error(f"MQ消息投递失败 [{routing_key}]: {e}")
        return False


# ──────────────────────────────────────────────
# AI 场景专用发布方法 (PRD 4.7/4.8/4.9)
# ──────────────────────────────────────────────

async def publish_ai_asr_task(
    audio_file_path: str,
    user_id: int,
    trace_id: Optional[str] = None,
) -> bool:
    """
    发布语音发单 AI 任务 (PRD 4.7: 语音→ASR→Dify NLP→创建订单)。
    """
    return await publish_message(
        routing_key=MQRoutingKey.AI_ASR_ORDER,
        payload={
            "audio_file_path": audio_file_path,
            "user_id": user_id,
        },
        event_type="ai.asr.requested",
        trace_id=trace_id,
    )


async def publish_ai_ocr_task(
    receipt_image_url: str,
    user_id: int,
    trace_id: Optional[str] = None,
) -> bool:
    """
    发布小票OCR AI 任务 (PRD 4.8: 小票图片→Dify OCR→创建现货订单)。
    """
    return await publish_message(
        routing_key=MQRoutingKey.AI_OCR_SPOT,
        payload={
            "receipt_image_url": receipt_image_url,
            "user_id": user_id,
        },
        event_type="ai.ocr.requested",
        trace_id=trace_id,
    )


async def publish_ai_dispute_task(
    dispute_id: int,
    order_id: int,
    applicant_id: int,
    description: str,
    image_urls: list[str],
    trace_id: Optional[str] = None,
) -> bool:
    """
    发布纠纷裁判 AI 任务 (PRD 4.9: 纠纷描述+图片→Dify多模态→判责)。
    """
    return await publish_message(
        routing_key=MQRoutingKey.AI_DISPUTE_JUDGE,
        payload={
            "dispute_id": dispute_id,
            "order_id": order_id,
            "applicant_id": applicant_id,
            "description": description,
            "image_urls": image_urls,
        },
        event_type="ai.dispute.requested",
        trace_id=trace_id,
    )


async def publish_order_notify(
    order_id: int,
    from_status: int,
    to_status: int,
    affected_user_ids: list[int],
    trace_id: Optional[str] = None,
) -> bool:
    """
    发布订单状态变更通知 (PRD 4.2/4.4: WebSocket推送 + 短信)。
    """
    return await publish_message(
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


async def publish_order_es_sync(
    order_id: int,
    action: str,  # "upsert" | "delete"
    doc: Optional[dict[str, Any]] = None,
    trace_id: Optional[str] = None,
) -> bool:
    """
    发布 ES 索引同步消息 (PRD 4.3: MySQL → ES 准实时同步)。
    """
    return await publish_message(
        routing_key=MQRoutingKey.ORDER_STATUS_SYNC,
        payload={
            "order_id": order_id,
            "action": action,
            "doc": doc,
        },
        event_type=f"es.sync.{action}",
        trace_id=trace_id,
    )


async def publish_reconcile_trigger(
    trace_id: Optional[str] = None,
) -> bool:
    """
    发布增量对账触发消息 (PRD 4.5: 每小时扫描近24h活跃订单)。
    由定时任务 scheduler 每小时调用。
    """
    return await publish_message(
        routing_key=MQRoutingKey.ORDER_RECONCILE,
        payload={
            "triggered_at": _now_iso(),
        },
        event_type="reconcile.triggered",
        trace_id=trace_id,
    )


async def publish_chat_cleanup_trigger(
    order_id: int,
    finished_at: str,
    trace_id: Optional[str] = None,
) -> bool:
    """
    发布阅后即焚清理消息 (PRD 3.1: 订单完成24h后物理销毁聊天记录)。
    """
    return await publish_message(
        routing_key=MQRoutingKey.ORDER_CHAT_CLEANUP,
        payload={
            "order_id": order_id,
            "finished_at": finished_at,
        },
        event_type="chat.cleanup.triggered",
        trace_id=trace_id,
    )


# ──────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────

def _now_dt() -> datetime:
    """返回当前 UTC 时间 (datetime对象, 用于 aio_pika Message.timestamp)。"""
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO8601 字符串 (用于 JSON 消息体)。"""
    return datetime.now(timezone.utc).isoformat()
