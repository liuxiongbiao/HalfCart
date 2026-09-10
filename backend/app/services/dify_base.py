"""
Dify 工作流 — 公共服务基类
============================
提取所有工作流共用的 HTTP 请求、异常兜底、置信度校验逻辑。
新增工作流只需继承此基类, 实现 _build_inputs() 和 _parse_output() 两个抽象方法。

架构:
  BaseDifyWorkflow (本文件)
    ├── TextParserWorkflow  (dify_text_parser.py)
    ├── (后续) OCRWorkflow  (dify_ocr.py)
    └── (后续) DisputeWorkflow (dify_dispute.py)

使用方式:
  wf = TextParserWorkflow()
  resp = await wf.run("帮我拼一盒瑞士卷, 3个人, 每人35")
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Generic, Optional, TypeVar

import aiohttp
from aiohttp import ClientTimeout
from pydantic import BaseModel

from app.config import settings
from app.schemas.dify import DifyWorkflowResponse

logger = logging.getLogger(__name__)

# ── 类型变量 ──
T = TypeVar("T", bound=BaseModel)


class BaseDifyWorkflow(ABC, Generic[T]):
    """
    Dify 工作流抽象基类。

    子类必须覆盖:
      - workflow_name:   Dify 工作流标识
      - api_key:         Dify API 密钥 (每个工作流独立)
      - input_variable:  Dify inputs 中的变量名
      - output_schema:   输出 Pydantic Schema 类
      - confidence_threshold:  置信度阈值
      - _build_inputs(): 将业务入参构造为 Dify inputs dict
      - _parse_output():  将 Dify outputs 解析为 output_schema
    """

    # ── 子类覆盖项 ──
    workflow_name: str = ""                           # Dify 工作流名称
    api_key: str = ""                                 # 独立 API Key
    input_variable: str = ""                          # Dify inputs 变量名
    output_schema: type[T] = None                     # 输出 Pydantic Schema
    confidence_threshold: float = 0.6                 # 默认阈值

    # ── 公共配置 (继承自全局 settings) ──
    @property
    def api_base(self) -> str:
        return settings.DIFY_API_BASE.rstrip("/")

    @property
    def timeout(self) -> int:
        return settings.DIFY_TIMEOUT_SECONDS

    # ══════════════════════════════════════════════
    # 抽象方法 (子类实现)
    # ══════════════════════════════════════════════

    @abstractmethod
    def _build_inputs(self, *args, **kwargs) -> dict:
        """将业务入参构造为 Dify 请求的 inputs dict。"""
        ...

    @abstractmethod
    def _parse_output(self, outputs: dict) -> T:
        """将 Dify 返回的 outputs dict 解析为 output_schema 实例。"""
        ...

    # ══════════════════════════════════════════════
    # 模板方法: 完整的请求→解析→校验流程
    # ══════════════════════════════════════════════

    async def run(self, *args, trace_id: Optional[str] = None, **kwargs) -> DifyWorkflowResponse[T]:
        """
        执行完整的工作流调用链。

        流程:
          1. _build_inputs()  → 构建请求参数
          2. _call_dify()     → HTTP POST Dify
          3. _parse_output()  → 解析为 Schema
          4. _validate()      → 业务二次校验
          5. 返回 DifyWorkflowResponse

        任何异常都不抛出, 降级为 fallback 响应。
        """
        try:
            inputs = self._build_inputs(*args, **kwargs)
            outputs = await self._call_dify(inputs, trace_id)
            data = self._parse_output(outputs)
            data = self._validate(data)

            if data.confidence >= self.confidence_threshold:
                return DifyWorkflowResponse.success(data)
            else:
                return DifyWorkflowResponse.confirm(
                    data=data,
                    message=f"解析置信度较低 ({data.confidence:.0%}), 请确认后提交",
                )
        except Exception as e:
            logger.error(
                f"[{self.workflow_name}] 工作流执行失败: {e}", exc_info=True
            )
            return DifyWorkflowResponse.fallback()

    # ══════════════════════════════════════════════
    # HTTP 请求 (子类可直接调用)
    # ══════════════════════════════════════════════

    async def _call_dify(self, inputs: dict, trace_id: Optional[str] = None) -> dict:
        """
        调用 Dify Workflow API — 含超时、异常捕获、降级。

        Returns:
            Dify data.outputs dict

        Raises:
            (内部捕获, 不向外抛出)
        """
        url = f"{self.api_base}/workflows/run"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "inputs": inputs,
            "response_mode": "blocking",
            "user": "halfcart-system",
        }

        logger.info(
            f"[{self.workflow_name}] 调用 Dify: "
            f"input_key={list(inputs.keys())} timeout={self.timeout}s"
        )

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=ClientTimeout(total=self.timeout),
                ) as resp:
                    return await self._handle_response(resp)
        except asyncio.TimeoutError:
            raise RuntimeError(f"[{self.workflow_name}] Dify 请求超时 ({self.timeout}s)")
        except aiohttp.ClientError as e:
            raise RuntimeError(f"[{self.workflow_name}] Dify 网络异常: {e}")

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> dict:
        """解析 HTTP 响应, 提取 outputs。"""
        import json

        body = await resp.text()

        if resp.status != 200:
            raise RuntimeError(
                f"[{self.workflow_name}] HTTP {resp.status}: {body[:200]}"
            )

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"[{self.workflow_name}] 响应非 JSON: {body[:200]}")

        if "error" in data:
            raise RuntimeError(f"[{self.workflow_name}] 业务错误: {data['error']}")

        outputs = data.get("data", {}).get("outputs", {})
        if not outputs:
            raise RuntimeError(f"[{self.workflow_name}] 无输出")

        logger.info(f"[{self.workflow_name}] Dify 调用成功")
        return outputs

    # ══════════════════════════════════════════════
    # 二次校验 (子类可覆盖)
    # ══════════════════════════════════════════════

    def _validate(self, data: T) -> T:
        """
        业务二次校验钩子。
        子类可覆盖此方法实现自定义校验逻辑 (修正非法值、下调置信度)。
        默认不做额外校验。
        """
        return data
