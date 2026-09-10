"""
拼单文本解析工作流 — 专属服务
==============================
PRD 4.7: 用户输入自然语言 → Dify NLP 解析 → 结构化订单参数。

二次校验规则 (PRD 4.7 分支流程):
  - 人数 < 2        → 自动修正为 2,  置信度上限 0.5
  - 价格 < 0        → 自动修正为 0,  置信度上限 0.5
  - 截单时间早于现在 → 置信度上限 0.5
  - 截单时间格式非法 → 清空字段,      置信度上限 0.5

配置隔离: 所有配置项前缀为 DIFY_TEXT_PARSER_*
"""

import logging
from datetime import datetime

from pydantic import BaseModel, Field

from app.config import settings
from app.schemas.dify import TextParserData
from app.services.dify_base import BaseDifyWorkflow

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 配置模型 (按工作流隔离)
# ══════════════════════════════════════════════


class TextParserConfig(BaseModel):
    """拼单文本解析工作流专属配置。"""

    api_key: str = Field(
        default="", description="Dify API Key (环境变量: DIFY_TEXT_PARSER_API_KEY)"
    )
    workflow_name: str = Field(
        default="text-order-parser", description="Dify 工作流名称"
    )
    input_variable: str = Field(
        default="user_input_text", description="Dify inputs 中的变量名"
    )
    confidence_threshold: float = Field(
        default=0.6, description="置信度阈值 (≥此值自动填充)"
    )

    @classmethod
    def from_settings(cls) -> "TextParserConfig":
        return cls(
            api_key=settings.DIFY_TEXT_PARSER_API_KEY.get_secret_value(),
            workflow_name=settings.DIFY_TEXT_PARSER_WORKFLOW,
            input_variable=settings.DIFY_TEXT_PARSER_INPUT_VAR,
            confidence_threshold=settings.DIFY_TEXT_PARSER_THRESHOLD,
        )


# ══════════════════════════════════════════════
# 工作流服务类 — 继承基类
# ══════════════════════════════════════════════


class TextParserWorkflow(BaseDifyWorkflow[TextParserData]):
    """
    拼单文本解析工作流。

    用法:
        wf = TextParserWorkflow()
        resp = await wf.run("山姆瑞士卷 3人拼 每人35 明天下午南门见")
        # resp.auto_fill → True/False
        # resp.data      → TextParserData
    """

    def __init__(self) -> None:
        cfg = TextParserConfig.from_settings()
        self.workflow_name = cfg.workflow_name
        self.api_key = cfg.api_key
        self.input_variable = cfg.input_variable
        self.confidence_threshold = cfg.confidence_threshold
        self.output_schema = TextParserData

    # ── 构造 Dify 请求 inputs ──

    def _build_inputs(self, user_text: str) -> dict:
        """
        将用户输入文本封装为 Dify 请求参数。

        Args:
            user_text: 用户输入的自然语言字符串

        Returns:
            {"user_input_text": "..."}
        """
        if not user_text or not user_text.strip():
            raise ValueError("输入文本为空")

        return {self.input_variable: user_text.strip()}

    # ── 解析 Dify 输出 ──

    def _parse_output(self, outputs: dict) -> TextParserData:
        """
        将 Dify 返回的 outputs 解析为 TextParserData。

        Dify 返回的典型结构:
          {
            "goods_name": "瑞士卷",
            "total_people": 3,
            "price_per_person": 35.0,
            "deadline": "2026-07-11 15:00",
            "meet_location": "南门",
            "purchase_channel": "山姆",
            "goods_desc": "",
            "confidence": 0.85
          }
        """
        return TextParserData(
            goods_name=str(outputs.get("goods_name", "")).strip(),
            total_people=int(outputs.get("total_people", 2)),
            price_per_person=float(outputs.get("price_per_person", 0)),
            deadline=str(outputs.get("deadline", "")).strip(),
            meet_location=str(outputs.get("meet_location", "")).strip(),
            purchase_channel=str(outputs.get("purchase_channel", "")).strip(),
            goods_desc=str(outputs.get("goods_desc", "")).strip(),
            confidence=float(outputs.get("confidence", 0)),
        )

    # ── 二次校验 (PRD 4.7 分支流程) ──

    def _validate(self, data: TextParserData) -> TextParserData:
        """
        后端二次校验规则。

        规则:
          1. 人数 < 2    → 修正为 2, 置信度上限 0.5
          2. 价格 < 0    → 修正为 0, 置信度上限 0.5
          3. 时间已过     → 置信度上限 0.5
          4. 时间格式非法 → 清空字段, 置信度上限 0.5
        """
        altered = False

        # ── 规则 1: 人数校验 ──
        if data.total_people < 2:
            logger.warning(
                f"[{self.workflow_name}] 人数 {data.total_people} < 2, 自动修正为 2"
            )
            data.total_people = 2
            data.confidence = min(data.confidence, 0.5)
            altered = True

        # ── 规则 2: 价格校验 ──
        if data.price_per_person < 0:
            logger.warning(
                f"[{self.workflow_name}] 价格 {data.price_per_person} < 0, 自动修正为 0"
            )
            data.price_per_person = 0.0
            data.confidence = min(data.confidence, 0.5)
            altered = True

        # ── 规则 3 + 4: 时间校验 ──
        if data.deadline:
            try:
                dt = datetime.strptime(data.deadline, "%Y-%m-%d %H:%M")
                if dt < datetime.now():
                    logger.warning(
                        f"[{self.workflow_name}] 截单时间 {data.deadline} 早于当前时间, "
                        f"置信度下调至 0.5"
                    )
                    data.confidence = min(data.confidence, 0.5)
                    altered = True
            except ValueError:
                logger.warning(
                    f"[{self.workflow_name}] 截单时间格式非法: '{data.deadline}', "
                    f"清空字段并下调置信度"
                )
                data.deadline = ""
                data.confidence = min(data.confidence, 0.5)
                altered = True

        if altered:
            logger.info(
                f"[{self.workflow_name}] 二次校验触发修正, "
                f"最终置信度: {data.confidence}"
            )

        return data


# ══════════════════════════════════════════════
# 模块级单例 (懒加载)
# ══════════════════════════════════════════════

_text_parser: TextParserWorkflow | None = None


def get_text_parser() -> TextParserWorkflow:
    """获取 TextParserWorkflow 单例。"""
    global _text_parser
    if _text_parser is None:
        _text_parser = TextParserWorkflow()
    return _text_parser
