"""
Dify 工作流异步统一客户端
==========================
对应 PRD 4.7/4.8/4.9: 三个 AI 场景:
  - 语音发单 NLP  (ASR → 结构化订单)
  - 小票 OCR 识别  (图片 → 商品列表)
  - 纠纷多模态裁判 (描述+图片 → 判责结果)
"""

import asyncio
import json
import logging
from typing import Any, Optional

import aiohttp
from aiohttp import ClientTimeout, FormData

from app.config import settings
from app.middleware.exception_handler import AppException
from app.schemas.common import ErrorCode

logger = logging.getLogger(__name__)

_WORKFLOW_RUN_PATH = "/workflows/run"


class DifyClient:
    """Dify 工作流异步 HTTP 客户端。"""

    def __init__(self, api_key: str = "") -> None:
        key = api_key or settings.DIFY_API_KEY.get_secret_value()
        if not key:
            logger.warning("DIFY_API_KEY 未配置 — AI 功能不可用")
        self._api_base = settings.DIFY_API_BASE.rstrip("/")
        self._api_key = key
        self._timeout = settings.DIFY_TIMEOUT_SECONDS

    @staticmethod
    def with_api_key(api_key: str) -> "DifyClient":
        return DifyClient(api_key=api_key)

    # ── 文件上传 ──

    async def upload_file(
        self, file_content: bytes, filename: str = "receipt.jpg",
        mime_type: str = "image/jpeg", user: str = "halfcart-system",
    ) -> dict:
        """
        上传文件到 Dify, 返回完整响应 dict (含 id, name, size, url 等)。
        Dify API: POST /files/upload (multipart/form-data)
        """
        url = f"{self._api_base}/files/upload"
        data = FormData()
        data.add_field("file", file_content, filename=filename, content_type=mime_type)
        data.add_field("user", user)

        logger.info(f"[Dify] 文件上传: name={filename} size={len(file_content)} bytes url={url}")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=data,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=ClientTimeout(total=30),
                ) as resp:
                    body = await resp.text()
                    logger.info(f"[Dify] 文件上传响应 HTTP {resp.status}: {body[:600]}")

                    if resp.status not in (200, 201):
                        raise AppException(
                            code=ErrorCode.AI_ASR_FAILED,
                            message=f"Dify 文件上传失败 HTTP {resp.status}: {body[:300]}",
                        )
                    result = json.loads(body)
                    if not result.get("id"):
                        raise AppException(
                            code=ErrorCode.AI_INVALID_RESPONSE,
                            message=f"Dify 文件上传返回缺少 id: {body[:300]}",
                        )
                    logger.info(f"[Dify] 文件上传成功: id={result['id']}")
                    return result
        except AppException:
            raise
        except aiohttp.ClientError as e:
            raise AppException(code=ErrorCode.AI_ASR_FAILED, message=f"Dify 文件上传网络异常: {e}")

    # ── 执行工作流 ──

    async def run_workflow(
        self, workflow_name: str, inputs: dict[str, Any],
        user: str = "halfcart-system", timeout: Optional[int] = None,
        trace_id: Optional[str] = None,
        files: Optional[list[dict]] = None,
    ) -> dict[str, Any]:
        """调用 Dify 工作流 API (JSON 模式)。

        Args:
            files: 顶层文件引用数组, Dify Workflow API 要求
                   [{"type":"image","transfer_method":"local_file","upload_file_id":"xxx"}]
        """
        url = f"{self._api_base}{_WORKFLOW_RUN_PATH}"
        timeout_sec = timeout or self._timeout
        payload: dict = {"inputs": inputs, "response_mode": "blocking", "user": user}
        if files:
            payload["files"] = files

        logger.info(f"[Dify] 工作流请求: url={url} payload={json.dumps(payload, ensure_ascii=False)[:800]}")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=ClientTimeout(total=timeout_sec),
                ) as resp:
                    return await self._handle_response(resp, workflow_name)
        except aiohttp.ClientError as e:
            raise await self._classify_network_error(e, workflow_name)
        except asyncio.TimeoutError:
            raise AppException(code=ErrorCode.AI_TIMEOUT, message=f"Dify 工作流超时 [{workflow_name}]: {timeout_sec}s")

    # ── 响应处理 ──

    async def _handle_response(self, resp: aiohttp.ClientResponse, workflow_name: str) -> dict[str, Any]:
        body = await resp.text()
        logger.info(f"[Dify] 工作流响应 HTTP {resp.status}: {body[:1000]}")

        if resp.status in (401, 403):
            raise AppException(code=ErrorCode.AI_AUTHENTICATION_FAILED, message=f"Dify 认证失败 [{workflow_name}]")
        if resp.status == 404:
            raise AppException(code=ErrorCode.AI_WORKFLOW_NOT_FOUND, message=f"Dify 工作流不存在 [{workflow_name}]")
        if resp.status == 429:
            raise AppException(code=ErrorCode.AI_RATE_LIMITED, message=f"Dify API 限流 [{workflow_name}]")
        if resp.status == 422:
            raise AppException(code=ErrorCode.AI_INVALID_RESPONSE, message=f"Dify 参数校验失败 [{workflow_name}]: {body[:300]}")
        if resp.status >= 500:
            raise AppException(code=ErrorCode.AI_ASR_FAILED, message=f"Dify 服务异常 [{workflow_name}] HTTP {resp.status}")

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise AppException(code=ErrorCode.AI_INVALID_RESPONSE, message=f"Dify 响应非 JSON [{workflow_name}]: {body[:300]}")

        if "error" in data:
            raise AppException(code=ErrorCode.AI_ASR_FAILED, message=f"Dify 工作流执行失败 [{workflow_name}]: {data['error']}")

        outputs = data.get("data", {}).get("outputs", {})
        if not outputs:
            outputs = data.get("outputs", data.get("data", {}))
        if isinstance(outputs, dict) and not outputs:
            outputs = {}
        if not outputs:
            raise AppException(code=ErrorCode.AI_INVALID_RESPONSE, message=f"Dify 工作流无输出 [{workflow_name}]: {body[:500]}")

        logger.info(f"[Dify] 工作流成功: workflow={workflow_name} output_keys={list(outputs.keys()) if isinstance(outputs, dict) else 'N/A'}")
        return outputs

    # ── 网络异常分类 ──

    async def _classify_network_error(self, error: aiohttp.ClientError, workflow_name: str) -> AppException:
        error_str = str(error)
        if "Timeout" in type(error).__name__ or "timeout" in error_str.lower():
            return AppException(code=ErrorCode.AI_TIMEOUT, message=f"Dify 请求超时 [{workflow_name}]")
        return AppException(code=ErrorCode.AI_ASR_FAILED, message=f"Dify 请求异常 [{workflow_name}]: {error_str[:200]}")


_dify_client: Optional[DifyClient] = None

def get_dify_client() -> DifyClient:
    global _dify_client
    if _dify_client is None:
        _dify_client = DifyClient()
    return _dify_client
