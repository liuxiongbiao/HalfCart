"""
RabbitMQ 消费者基类
==================
封装连接管理、自动重连、优雅关闭、手动ACK、死信队列、幂等去重。

底座设计 4.1-4.4:
  - Topic交换机: halfcart.exchange
  - 7个业务队列 + 5个死信队列
  - 统一消息格式: {event_id, event_type, timestamp, source, payload, trace_id}

特性:
  - aio-pika connect_robust 自动重连
  - prefetch_count=1 逐条消费, 手动 ACK
  - 失败重试 3 次 → nack requeue → 超限进死信队列
  - event_id 幂等去重 (Redis SETNX 30min TTL)
  - 优雅关闭 (SIGINT / SIGTERM)
"""

import asyncio
import json
import logging
import signal
from typing import Any, Awaitable, Callable, Optional, Set

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractConnection,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)

from app.config import settings
from app.core.mq_client import MQRoutingKey, get_exchange
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────

MAX_RETRY_COUNT = 3                # 最大重试次数
IDEMPOTENT_TTL = 1800              # 幂等去重 TTL (30分钟)
PREFETCH_COUNT = 1                 # 每次预取1条

# 重试计数 header key (写入消息 header, 跨 nack 持久化)
RETRY_HEADER = "x-retry-count"


class BaseConsumer:
    """
    RabbitMQ 消费者基类。

    子类只需实现 handle_message(payload, message) 方法。
    基类负责: 连接、ACK/NACK、重试、死信、幂等。

    Usage:
        class MyConsumer(BaseConsumer):
            queue_name = MQRoutingKey.ORDER_STATUS_SYNC

            async def handle_message(self, payload, message):
                await upsert_order_doc(payload["order_id"], payload["doc"])

        consumer = MyConsumer()
        await consumer.start()
    """

    # 子类必须覆盖
    queue_name: str = ""

    # 可选覆盖
    prefetch_count: int = PREFETCH_COUNT

    def __init__(self) -> None:
        if not self.queue_name:
            raise ValueError(f"{self.__class__.__name__}.queue_name 必须设置")
        self._connection: Optional[AbstractRobustConnection] = None
        self._channel: Optional[AbstractChannel] = None
        self._queue: Optional[AbstractQueue] = None
        self._running = False
        # 本地幂等兜底 (Redis 为主, 内存为辅)
        self._local_dedup: Set[str] = set()

    # ──────────────────────────────────────────
    # 启动 / 关闭
    # ──────────────────────────────────────────

    async def start(self) -> None:
        """启动消费者: 建立连接 → 绑定队列 → 开始消费。"""
        logger.info(f"[{self.queue_name}] 消费者启动中...")

        self._connection = await aio_pika.connect_robust(
            settings.RABBITMQ_URL,
            reconnect_interval=settings.MQ_RECONNECT_INTERVAL,
        )

        self._channel = await self._connection.channel()
        await self._channel.set_qos(prefetch_count=self.prefetch_count)

        # 声明队列 (幂等, 已存在则复用)
        self._queue = await self._channel.declare_queue(
            name=self.queue_name,
            durable=True,
        )

        # 绑定消费者标签
        self._consumer_tag = await self._queue.consume(self._on_message)

        self._running = True
        # 注册信号处理
        self._register_signals()

        logger.info(f"[{self.queue_name}] 消费者启动完成, 等待消息...")

        # 保持运行直到收到停止信号
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self._graceful_shutdown()

    async def _graceful_shutdown(self) -> None:
        """优雅关闭: 停止消费 → 关闭 channel → 关闭连接。"""
        logger.info(f"[{self.queue_name}] 正在优雅关闭...")
        self._running = False
        try:
            if self._queue and self._consumer_tag:
                await self._queue.cancel(self._consumer_tag)
        except Exception:
            pass
        try:
            if self._channel:
                await self._channel.close()
        except Exception:
            pass
        try:
            if self._connection:
                await self._connection.close()
        except Exception:
            pass
        logger.info(f"[{self.queue_name}] 已关闭")

    def _register_signals(self) -> None:
        """注册 SIGINT / SIGTERM 信号处理, 触发优雅关闭。"""
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._signal_stop)
            except NotImplementedError:
                # Windows 不支持 add_signal_handler
                pass

    def _signal_stop(self) -> None:
        logger.info(f"[{self.queue_name}] 收到停止信号")
        self._running = False

    # ──────────────────────────────────────────
    # 消息处理核心
    # ──────────────────────────────────────────

    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        """
        消息回调 (由 aio_pika 调用)。

        流程:
          1. 幂等去重 (event_id)
          2. 解析 payload
          3. 调用子类 handle_message
          4. 成功 → ACK
          5. 失败 → 重试判断 → NACK (requeue) 或 REJECT (进死信)
        """
        async with message.process(requeue=False):
            body = message.body
            try:
                raw = json.loads(body.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.error(f"[{self.queue_name}] 消息体非 JSON, 直接拒绝: {body[:100]}")
                await message.reject(requeue=False)
                return

            event_id = raw.get("event_id", "")
            trace_id = raw.get("trace_id", "N/A")

            # ── 幂等去重 ──────────────────────
            if await self._is_duplicate(event_id):
                logger.info(f"[{self.queue_name}] 幂等跳过: event_id={event_id}")
                return

            payload = raw.get("payload", {})
            retry_count = self._get_retry_count(message)

            logger.info(
                f"[{self.queue_name}] 收到消息: "
                f"event_id={event_id} event_type={raw.get('event_type')} "
                f"retry={retry_count}/{MAX_RETRY_COUNT} trace={trace_id}"
            )

            try:
                await self.handle_message(payload, message)
                # 成功 → 标记幂等 + ACK
                await self._mark_processed(event_id)
                logger.debug(f"[{self.queue_name}] 处理成功: event_id={event_id}")
            except Exception as e:
                logger.error(
                    f"[{self.queue_name}] 处理失败: event_id={event_id} "
                    f"retry={retry_count} error={e}",
                    exc_info=True,
                )

                retry_count += 1
                if retry_count <= MAX_RETRY_COUNT:
                    # 重试: nack + requeue (带重试 header)
                    logger.warning(
                        f"[{self.queue_name}] 第{retry_count}次重试: event_id={event_id}"
                    )
                    headers = dict(message.headers or {})
                    headers[RETRY_HEADER] = str(retry_count)
                    # 重新发布到同一队列 (保留 header)
                    exchange = get_exchange()
                    await exchange.publish(
                        aio_pika.Message(
                            body=body,
                            headers=headers,
                            content_type="application/json",
                            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                            message_id=event_id,
                        ),
                        routing_key=self.queue_name,
                    )
                    # 原消息 ACK (不 requeue, 因为已重新发布)
                else:
                    # 超过重试次数 → REJECT 不 requeue → 进入死信队列
                    logger.error(
                        f"[{self.queue_name}] 超过最大重试次数, 进入死信: "
                        f"event_id={event_id}"
                    )
                    await message.reject(requeue=False)

    # ──────────────────────────────────────────
    # 子类覆盖: 业务逻辑
    # ──────────────────────────────────────────

    async def handle_message(
        self, payload: dict[str, Any], message: AbstractIncomingMessage
    ) -> None:
        """
        处理消息的钩子方法 — 子类必须实现。

        Args:
            payload: 消息的 payload 字段 (dict)
            message: 原始 aio_pika 消息对象
        """
        raise NotImplementedError

    # ──────────────────────────────────────────
    # 幂等去重 (Redis SETNX + 本地内存兜底)
    # ──────────────────────────────────────────

    async def _is_duplicate(self, event_id: str) -> bool:
        """检查 event_id 是否已处理过。"""
        if not event_id:
            return False
        # 本地快速检查
        if event_id in self._local_dedup:
            return True
        # Redis 分布式去重
        try:
            r = get_redis()
            key = f"halfcart:consumer:dedup:{self.queue_name}:{event_id}"
            was_set = await r.set(key, "1", nx=True, ex=IDEMPOTENT_TTL)
            if not was_set:
                self._local_dedup.add(event_id)
                return True
            return False
        except Exception:
            # Redis 不可用时降级为本地去重
            return event_id in self._local_dedup

    async def _mark_processed(self, event_id: str) -> None:
        """标记消息已处理 (写入本地缓存)。"""
        if event_id:
            self._local_dedup.add(event_id)

    # ──────────────────────────────────────────
    # 重试计数
    # ──────────────────────────────────────────

    def _get_retry_count(self, message: AbstractIncomingMessage) -> int:
        """从消息 header 读取当前重试次数。"""
        headers = message.headers or {}
        try:
            return int(headers.get(RETRY_HEADER, 0))
        except (ValueError, TypeError):
            return 0
