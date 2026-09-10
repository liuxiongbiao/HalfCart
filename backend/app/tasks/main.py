"""
消费者独立进程入口
====================
生产环境与 API 服务分离部署，专注消费 MQ 消息 + 运行定时任务。

启动:
    python -m app.tasks.main
    uvicorn app.tasks.main:app --host 0.0.0.0 --port 8001  # FastAPI 模式, 仅健康检查
"""

import asyncio
import logging
import signal
import sys

from app.tasks import start_all_consumers, stop_all_consumers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("consumer")


async def main():
    """启动全部消费者 + 调度器, 等待进程信号后优雅关闭。"""
    logger.info("=" * 50)
    logger.info("HalfCart 消费者进程启动中...")
    await start_all_consumers()

    # 阻塞直到收到 SIGINT / SIGTERM
    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, stop_event.set)
        loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    except NotImplementedError:
        pass  # Windows 不支持 add_signal_handler

    logger.info("消费者进程运行中, 等待信号...")
    await stop_event.wait()
    logger.info("收到停止信号, 正在关闭...")
    await stop_all_consumers()
    logger.info("消费者进程已关闭")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("收到中断信号, 退出")
        sys.exit(0)
