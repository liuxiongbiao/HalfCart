"""
消费者 & 定时任务统一入口
===========================
启动所有 MQ 消费者 + APScheduler 定时任务。

消费者进程与 FastAPI 服务独立运行, 互不阻塞。
开发环境可与 API 同容器启动, 生产环境建议独立部署。

启动方式:
    # 方式 1: 在 main.py lifespan 中调用
    from app.tasks import start_all_consumers, stop_all_consumers

    # 方式 2: 独立进程
    python -m app.tasks

注意: 消费者依赖 Redis 连接池和 ES 客户端, 启动前需确保中间件已就绪。
"""

import asyncio
import logging
from typing import List

from app.core.mq_client import MQRoutingKey
from app.tasks.ai_task_consumer import AITaskConsumer
from app.tasks.chat_cleanup_consumer import ChatCleanupConsumer
from app.tasks.consumer_base import BaseConsumer
from app.tasks.es_sync_consumer import ESOrderSyncConsumer
from app.tasks.notify_consumer import OrderNotifyConsumer
from app.tasks.scheduler import shutdown_scheduler, start_scheduler

logger = logging.getLogger(__name__)

# 全局消费者实例列表
_consumers: List[BaseConsumer] = []
_consumer_tasks: List[asyncio.Task] = []


def _create_consumers() -> List[BaseConsumer]:
    """创建全部消费者实例。"""
    return [
        # ES 索引同步 (PRD 4.3)
        ESOrderSyncConsumer(),
        # 订单通知 (PRD 4.2/4.4)
        OrderNotifyConsumer(),
        # 阅后即焚 — 聊天清理 (PRD 4.4)
        ChatCleanupConsumer(),
        # AI 任务 — 小票 OCR (PRD 4.8)
        AITaskConsumer(queue_name=MQRoutingKey.AI_OCR_SPOT),
        # AI 任务 — 纠纷裁判 (PRD 4.9)
        AITaskConsumer(queue_name=MQRoutingKey.AI_DISPUTE_JUDGE),
    ]


async def start_all_consumers() -> None:
    """
    启动所有 MQ 消费者 + APScheduler。

    每个消费者在独立的 asyncio.Task 中运行, 互不影响。
    由 main.py lifespan 启动阶段调用。
    """
    global _consumers, _consumer_tasks

    logger.info("=" * 50)
    logger.info("后台任务启动中...")

    _consumers = _create_consumers()
    _consumer_tasks = []

    for consumer in _consumers:
        task = asyncio.create_task(consumer.start(), name=consumer.queue_name)
        _consumer_tasks.append(task)
        logger.info(f"  ▶ 消费者已启动: {consumer.queue_name}")

    # 启动定时任务调度器
    start_scheduler()

    logger.info(f"全部 {len(_consumers)} 个消费者 + 调度器 已启动")
    logger.info("=" * 50)


async def stop_all_consumers() -> None:
    """
    停止所有消费者 + 调度器。

    优雅关闭: 取消所有 Task → 等待完成 → 清理。
    由 main.py lifespan 关闭阶段调用。
    """
    global _consumers, _consumer_tasks

    logger.info("后台任务正在关闭...")

    # 停止调度器
    shutdown_scheduler()

    # 停止消费者 (通过设置 _running=False)
    for consumer in _consumers:
        consumer._running = False

    # 取消所有 Task
    for task in _consumer_tasks:
        if not task.done():
            task.cancel()

    # 等待 Task 完成
    if _consumer_tasks:
        await asyncio.gather(*_consumer_tasks, return_exceptions=True)

    _consumers.clear()
    _consumer_tasks.clear()

    logger.info("全部后台任务已关闭")


# ══════════════════════════════════════════════
# 独立进程入口
# ══════════════════════════════════════════════

if __name__ == "__main__":
    """
    独立进程启动模式:
      python -m app.tasks

    适用于:
      - 开发调试时单独测试消费者
      - 生产环境将消费者独立部署为单独容器
    """
    import signal as _signal
    import sys as _sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async def _main():
        await start_all_consumers()
        # 保持运行直到收到停止信号
        stop_event = asyncio.Event()
        loop = asyncio.get_event_loop()
        try:
            loop.add_signal_handler(_signal.SIGINT, stop_event.set)
            loop.add_signal_handler(_signal.SIGTERM, stop_event.set)
        except NotImplementedError:
            pass
        await stop_event.wait()
        await stop_all_consumers()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("收到中断信号, 退出")
        _sys.exit(0)
