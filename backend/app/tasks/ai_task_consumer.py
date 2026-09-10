"""
AI 任务消费者 — 统一消费 AI 相关队列
=====================================
PRD 4.7/4.8/4.9: AI 异步解耦, 避免阻塞 API 响应。

消费队列 (3个):
  - ai.asr.order      → 语音发单 NLP  → parse_order_from_text()
  - ai.ocr.spot       → 小票 OCR      → parse_receipt_ocr()
  - ai.dispute.judge  → 纠纷多模态裁判 → judge_dispute()

处理流程:
  1. 消费 MQ 消息
  2. 调用对应 Dify 工作流
  3. AI 返回结果后更新订单/数据库
  → 失败重试3次 → 进死信队列
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from aio_pika.abc import AbstractIncomingMessage

from app.core.mq_client import MQRoutingKey
from app.services.dify_service import (
    parse_order_from_text,
    parse_receipt_ocr,
)
from app.tasks.consumer_base import BaseConsumer

logger = logging.getLogger(__name__)


class AITaskConsumer(BaseConsumer):
    """
    AI 任务统一消费者。
    根据队列路由自动分发到对应的 Dify 工作流。

    支持两个子队列, 共用一个消费者实例:
      - ai.ocr.spot
      - ai.dispute.judge

    注: ai.asr.order 已废弃 (V3.2: 语音发单改为同步 REST API)。
    """

    # ── 路由 → 处理器映射 ──
    _HANDLER_MAP = {
        MQRoutingKey.AI_OCR_SPOT: "_handle_ocr",
        MQRoutingKey.AI_DISPUTE_JUDGE: "_handle_dispute",
    }

    def __init__(self, queue_name: str) -> None:
        """
        根据队列名创建消费者。

        Args:
            queue_name: 必传, 需是 {AI_OCR_SPOT, AI_DISPUTE_JUDGE} 之一。
        """
        if queue_name not in self._HANDLER_MAP:
            raise ValueError(
                f"不支持的 AI 队列: {queue_name}, "
                f"有效值: {list(self._HANDLER_MAP.keys())}"
            )
        self.queue_name = queue_name
        super().__init__()

    async def handle_message(
        self, payload: dict[str, Any], message: AbstractIncomingMessage
    ) -> None:
        """根据队列路由分发到对应处理器。"""
        handler_name = self._HANDLER_MAP.get(self.queue_name)
        if not handler_name:
            raise RuntimeError(f"未找到处理器: {self.queue_name}")
        handler = getattr(self, handler_name)
        await handler(payload, message)

    # ──────────────────────────────────────────
    # [已废弃] ASR 语音发单 (PRD 4.7) — 改为同步 REST API
    # ──────────────────────────────────────────

    async def _handle_asr(
        self, payload: dict[str, Any], message: AbstractIncomingMessage
    ) -> None:
        """[已废弃] 语音发单改由 POST /api/v1/orders/speech-to-order 同步处理。"""
        logger.warning(f"[{self.queue_name}] _handle_asr 已废弃, 消息将被忽略")
        return

        # 低置信度提示用户确认
        threshold = 0.80
        if result.confidence < threshold:
            logger.warning(
                f"[{self.queue_name}] NLP 置信度 {result.confidence} < {threshold}, "
                f"需用户确认: user={user_id}"
            )

        logger.info(
            f"[{self.queue_name}] NLP 解析完成: user={user_id} "
            f"goods={result.goods_name} confidence={result.confidence}"
        )

    # ──────────────────────────────────────────
    # OCR 现货转让 (PRD 4.8)
    # ──────────────────────────────────────────

    async def _handle_ocr(
        self, payload: dict[str, Any], message: AbstractIncomingMessage
    ) -> None:
        """
        小票 OCR 识别 → 生成现货订单。

        payload:
          { "receipt_image_url": "https://...", "user_id": 1001 }

        流程:
          1. Dify OCR 识别 → 提取商品名/原价/数量
          2. 校验转让价格 ≤ 原价
          3. 创建 DELIVERING 现货订单
          4. 推送结果通知
        """
        image_url = payload.get("receipt_image_url", "")
        user_id = payload.get("user_id")
        trace_id = payload.get("trace_id")

        if not image_url:
            logger.warning(f"[{self.queue_name}] OCR 消息缺少 image_url, 跳过")
            return

        logger.info(
            f"[{self.queue_name}] 开始 OCR 识别: user={user_id} "
            f"image={image_url[:80]}... trace={trace_id}"
        )

        result = await parse_receipt_ocr(image_url, trace_id=trace_id)

        # OCR 返回商品列表, 逐条创建现货订单 (最多5单, PRD 4.8)
        if result.items:
            from app.core.database import async_session_factory
            from app.services.order_service import create_spot_order

            max_items = min(len(result.items), 5)  # PRD 4.8: 最多拆分5单
            async with async_session_factory() as session:
                for i, item in enumerate(result.items[:max_items]):
                    try:
                        transfer_price = Decimal(str(item.total_price))
                        original_price = Decimal(str(item.total_price))
                        order = await create_spot_order(
                            session=session,
                            user_id=user_id,
                            goods_name=item.item_name,
                            transfer_price=transfer_price,
                            receipt_img=image_url,
                            original_price=original_price,
                            goods_desc=f"OCR识别: 单价¥{item.unit_price} 数量{item.quantity}",
                            trace_id=trace_id,
                        )
                        logger.info(
                            f"[{self.queue_name}] OCR现货订单#{i+1}创建成功: "
                            f"order_id={order.id} goods={item.item_name} "
                            f"price={item.total_price}"
                        )
                    except Exception as create_err:
                        logger.error(
                            f"[{self.queue_name}] OCR现货订单#{i+1}创建失败: "
                            f"user={user_id} goods={item.item_name} error={create_err}"
                        )
        else:
            logger.warning(
                f"[{self.queue_name}] OCR 未识别到商品: user={user_id}"
            )

        logger.info(
            f"[{self.queue_name}] OCR 识别完成: user={user_id} "
            f"共 {len(result.items)} 件商品"
        )

    # ──────────────────────────────────────────
    # 纠纷裁判 (PRD 4.9)
    # ──────────────────────────────────────────

    async def _handle_dispute(
        self, payload: dict[str, Any], message: AbstractIncomingMessage
    ) -> None:
        """
        AI 纠纷裁判 → 自动判责 → 执行退款/结算。

        调用 dispute_service.execute_ai_judge() 执行全流程:
          Dify裁判 → 置信度判断 → 自动资金划转/转人工
        """
        from app.core.database import async_session_factory
        from app.services.dispute_service import execute_ai_judge

        dispute_id = payload.get("dispute_id")
        trace_id = payload.get("trace_id")

        if not dispute_id:
            logger.error(f"[{self.queue_name}] 纠纷消息缺少 dispute_id")
            return

        logger.info(
            f"[{self.queue_name}] 开始纠纷裁判: dispute={dispute_id} trace={trace_id}"
        )

        async with async_session_factory() as session:
            await execute_ai_judge(session, dispute_id, trace_id=trace_id)

        logger.info(f"[{self.queue_name}] 纠纷裁判完成: dispute={dispute_id}")
