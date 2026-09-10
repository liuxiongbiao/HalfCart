"""
Dify AI 工作流业务服务
======================
对应 PRD 三个 AI 场景:
  - 4.7 语音发单 NLP:  音频/文本 → 结构化订单参数
  - 4.8 小票 OCR 识别:  图片 → 商品列表 + 原价
  - 4.9 纠纷多模态裁判: 描述+图片 → 判责结果

每个方法:
  1. 调用 DifyClient.run_workflow()
  2. 校验置信度 (低于阈值进入人工处理)
  3. 解析为强类型 Pydantic Schema
  4. 失败时返回标准化错误
"""

import base64
import logging
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.config import settings
from app.core.dify_client import DifyClient, get_dify_client
from app.middleware.exception_handler import AppException
from app.schemas.common import ErrorCode

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 结构化输出 Schema (Pydantic)
# ══════════════════════════════════════════════


class ASROrderOutput(BaseModel):
    """语音发单 NLP 解析输出 (PRD 4.7)。

    Dify 工作流返回的5个必填字段 + 2个可选字段。
    """

    goods_name: str = Field(..., description="商品名称 (必填, ≤50字符)")
    total_people: int = Field(..., description="需要人数 (含团长, ≥2)")
    price_per_person: Decimal = Field(..., description="人均价格 (元)")
    deadline: str = Field(..., description="截单时间 (ISO8601 字符串)")
    meet_location: str = Field(..., description="交接地点")
    goods_desc: str = Field(default="", description="商品描述 (可选)")
    purchase_channel: str = Field(default="", description="采购渠道 (可选, 如山姆)")
    confidence: float = Field(default=0.0, description="NLP 整体置信度 0.0-1.0")


class OCRItem(BaseModel):
    """小票 OCR 单条商品识别输出 (PRD 4.8)。

    Dify 工作流返回的数组元素格式:
      { "item_name": "香菜", "unit_price": 3.16, "total_price": 0.60, "quantity": 0.19 }
    """

    item_name: str = Field(..., description="商品名称")
    unit_price: float = Field(default=0.0, description="单价 (元)")
    total_price: float = Field(default=0.0, description="总价 (元)")
    quantity: float = Field(default=0.0, description="数量 (kg/份)")


class OCROutput(BaseModel):
    """小票 OCR 识别完整输出 (PRD 4.8)。"""

    items: list[OCRItem] = Field(default_factory=list, description="商品列表")
    raw_text: str = Field(default="", description="OCR原始输出 (调试用)")
    debug_info: dict = Field(default_factory=dict, description="调试信息 (前端可展示)")


class DisputeJudgeOutput(BaseModel):
    """纠纷多模态裁判输出 (PRD 4.9)。

    AI 核验证据后给出的判责方案。
    """

    responsible_party: str = Field(
        ..., description="责任方: 'creator' (团长全责) / 'participant' (拼友全责) / 'shared' (双方)"
    )
    creator_ratio: float = Field(
        default=0.5, description="团长责任比例 (0.0 ~ 1.0, 双方分担时有效)"
    )
    participant_ratio: float = Field(
        default=0.5, description="拼友责任比例 (0.0 ~ 1.0)"
    )
    reason: str = Field(default="", description="判责理由 (AI生成的文字说明)")
    confidence: float = Field(default=0.0, description="判责置信度 0.0-1.0")


# ══════════════════════════════════════════════
# 工作流名称工具
# ══════════════════════════════════════════════

def _wf(name: str) -> str:
    """拼接完整工作流名称 (可加前缀)。"""
    return name


# ══════════════════════════════════════════════
# 工作流 1: 语音发单 NLP
# PRD 4.7: 语音转文字 → Dify NLP → 结构化订单
# ══════════════════════════════════════════════


async def parse_order_from_text(
    text: str,
    *,
    trace_id: Optional[str] = None,
) -> ASROrderOutput:
    """
    语音发单 NLP 解析 — 将自然语言文本转为结构化订单参数。

    PRD 4.7 主流程:
      1. 用户语音 → ASR 转写为文本 (由 MQ consumer 处理)
      2. 文本透传 Dify NLP 工作流 → 返回结构化 JSON
      3. 校验字段完整性 → 返回 ASROrderOutput

    Args:
        text:     转写后的自然语言文本 (如 "山姆瑞士卷一盒 3个人拼 每人35 明天下午3点南门见")
        trace_id: 全链路追踪ID

    Returns:
        ASROrderOutput 结构化订单参数

    Raises:
        AppException: AI_NLP_PARSE_FAILED / AI_LOW_CONFIDENCE / AI_TIMEOUT
    """
    if not text or not text.strip():
        raise AppException(
            code=ErrorCode.AI_NLP_PARSE_FAILED,
            message="语音转写文本为空，无法解析",
        )

    client = get_dify_client()
    workflow = settings.DIFY_WORKFLOW_ASR
    threshold = settings.DISPUTE_JUDGE_CONFIDENCE_THRESHOLD  # 通用80%阈值

    logger.info(f"语音发单 NLP: text_len={len(text)} workflow={workflow}")

    outputs = await client.run_workflow(
        workflow_name=workflow,
        inputs={"text": text.strip()},
        trace_id=trace_id,
    )

    # 校验置信度 (PRD 4.7: 低于80%进入人工确认)
    confidence = float(outputs.get("confidence", 0))
    if confidence < threshold:
        logger.warning(
            f"NLP 置信度不足: confidence={confidence} threshold={threshold} "
            f"→ 提示用户人工确认 (PRD 4.7兜底方案)"
        )
        # 仍然返回解析结果, 由调用方引导用户确认

    try:
        result = ASROrderOutput(
            goods_name=outputs.get("goods_name", ""),
            total_people=int(outputs.get("total_people", 0)),
            price_per_person=Decimal(str(outputs.get("price_per_person", 0))),
            deadline=outputs.get("deadline", ""),
            meet_location=outputs.get("meet_location", ""),
            goods_desc=outputs.get("goods_desc", ""),
            purchase_channel=outputs.get("purchase_channel", ""),
            confidence=confidence,
        )
    except (ValueError, TypeError) as e:
        raise AppException(
            code=ErrorCode.AI_NLP_PARSE_FAILED,
            message=f"NLP 输出字段校验失败: {e} — raw={str(outputs)[:200]}",
        )

    # 必填字段校验 (PRD 4.7: 5个必填字段)
    missing = []
    if not result.goods_name:
        missing.append("商品名称")
    if result.total_people < 2:
        missing.append("需要人数(≥2)")
    if result.price_per_person <= 0:
        missing.append("人均价格")
    if not result.deadline:
        missing.append("截单时间")
    if not result.meet_location:
        missing.append("交接地点")

    if missing:
        raise AppException(
            code=ErrorCode.AI_NLP_PARSE_FAILED,
            message=f"NLP 解析缺少必填字段: {', '.join(missing)} — 请补充后重试 (PRD 4.7)",
        )

    logger.info(
        f"语音发单 NLP 成功: goods={result.goods_name} "
        f"people={result.total_people} price={result.price_per_person} "
        f"confidence={confidence}"
    )
    return result


# ══════════════════════════════════════════════
# 工作流 2: 小票 OCR 识别
# PRD 4.8: 购物小票图片 → Dify 多模态 OCR → 商品信息
# ══════════════════════════════════════════════


async def parse_receipt_ocr(
    image_url: str,
    *,
    trace_id: Optional[str] = None,
) -> OCROutput:
    """
    小票 OCR 识别 — 从购物小票图片提取商品列表。

    PRD 4.8: 小票图片 → Dify 多模态 OCR → 商品数组。

    Dify 返回格式 (数组, 每个元素是一个商品):
      [
        {"item_name": "香菜", "unit_price": 3.16, "total_price": 0.60, "quantity": 0.19},
        ...
      ]

    Args:
        image_url: 小票图片 URL
        trace_id:  全链路追踪ID

    Returns:
        OCROutput (items: 商品列表)

    Raises:
        AppException: AI_OCR_FAILED
    """
    import json as _json

    debug_log = []  # 收集调试信息, 最终返回给前端

    if not image_url:
        raise AppException(code=ErrorCode.AI_OCR_FAILED, message="小票图片 URL 为空")

    ocr_api_key = settings.DIFY_OCR_API_KEY.get_secret_value() or settings.DIFY_API_KEY.get_secret_value()
    client = DifyClient(api_key=ocr_api_key)
    workflow = settings.DIFY_WORKFLOW_OCR
    input_key = settings.DIFY_OCR_INPUT_VAR
    workflow_inputs: dict = {}
    workflow_files: Optional[list] = None

    debug_log.append(f"workflow={workflow} input_key={input_key} api_key_prefix={ocr_api_key[:8]}...")

    if image_url.startswith("data:"):
        try:
            header, encoded = image_url.split(",", 1)
            mime = "image/jpeg"
            if "png" in header: mime = "image/png"
            ext = mime.split("/")[1]
            raw_bytes = base64.b64decode(encoded)
            debug_log.append(f"base64解码: {len(raw_bytes)} bytes, mime={mime}")

            # 上传到 Dify
            upload_result = await client.upload_file(
                file_content=raw_bytes, filename=f"receipt.{ext}", mime_type=mime,
            )
            file_id = upload_result.get("id", "")
            file_url = upload_result.get("url", "")
            debug_log.append(f"upload成功: file_id={file_id} url={file_url[:80] if file_url else 'N/A'}")

            # Dify Workflow API: 文件引用放在 inputs 里
            workflow_inputs[input_key] = {
                "transfer_method": "local_file",
                "upload_file_id": file_id,
                "type": "image",
            }
            debug_log.append(f"文件引用: inputs[{input_key}]={{transfer_method:local_file, upload_file_id:{file_id}}}")
        except AppException:
            raise
        except Exception as e:
            raise AppException(code=ErrorCode.AI_OCR_FAILED, message=f"小票图片处理失败: {e}")
    else:
        workflow_inputs[input_key] = image_url
        debug_log.append(f"直接传URL: {image_url[:80]}...")

    # 调用工作流 (文件引用在顶层 files 数组)
    debug_log.append(f"调用run_workflow: inputs={_json.dumps(workflow_inputs, ensure_ascii=False)[:300]} files={_json.dumps(workflow_files if 'workflow_files' in dir() else [], ensure_ascii=False)[:200]}")
    outputs = await client.run_workflow(
        workflow_name=workflow,
        inputs=workflow_inputs,
        files=workflow_files,
        timeout=120,  # OCR 工作流需要较长时间处理图片
        trace_id=trace_id,
    )

    # 解析 outputs
    debug_log.append(f"outputs_keys={list(outputs.keys()) if isinstance(outputs, dict) else type(outputs).__name__}")
    debug_log.append(f"outputs_raw={_json.dumps(outputs, ensure_ascii=False)[:600]}")

    raw_items = outputs.get("items", outputs) if isinstance(outputs, dict) else outputs
    debug_log.append(f"raw_items type={type(raw_items).__name__}")

    # 兼容各种 Dify 输出格式
    if isinstance(raw_items, dict) and len(raw_items) == 1:
        only_val = list(raw_items.values())[0]
        debug_log.append(f"单key={list(raw_items.keys())[0]}, val_type={type(only_val).__name__}")
        if isinstance(only_val, str):
            stripped = only_val.strip()
            if stripped.startswith("[") or stripped.startswith("{"):
                try:
                    raw_items = _json.loads(stripped)
                    debug_log.append("单key JSON字符串→解析成功")
                except _json.JSONDecodeError:
                    debug_log.append(f"单key JSON解析失败: {stripped[:100]}")
                    raw_items = only_val
            else:
                raw_items = [raw_items]  # 字符串但不是JSON,包装
        else:
            raw_items = only_val
    elif isinstance(raw_items, str):
        stripped = raw_items.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                raw_items = _json.loads(stripped)
                debug_log.append("字符串顶层→JSON解析成功")
            except _json.JSONDecodeError:
                debug_log.append(f"字符串顶层JSON解析失败: {stripped[:100]}")
                raw_items = []
        else:
            raw_items = [{"item_name": stripped, "unit_price": 0, "total_price": 0, "quantity": 1}]
    elif isinstance(raw_items, dict):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        debug_log.append(f"raw_items最终不是list: {type(raw_items).__name__}")
        raw_items = []

    debug_log.append(f"解析后商品数: {len(raw_items)}")

    if not raw_items:
        raise AppException(
            code=ErrorCode.AI_OCR_FAILED,
            message="OCR 未能识别任何商品 — 图片可能模糊或非小票 (PRD 4.8)",
        )

    items = []
    for raw in raw_items:
        try:
            items.append(OCRItem(
                item_name=str(raw.get("item_name", "")).strip(),
                unit_price=float(raw.get("unit_price", 0)),
                total_price=float(raw.get("total_price", 0)),
                quantity=float(raw.get("quantity", 0)),
            ))
        except (ValueError, TypeError) as e:
            logger.warning(f"OCR 单条解析跳过: {raw} error={e}")

    if not items:
        raise AppException(
            code=ErrorCode.AI_OCR_FAILED,
            message="OCR 商品列表解析失败 — 字段格式不匹配",
        )

    logger.info(f"小票 OCR 成功: {len(items)} 件商品")
    return OCROutput(
        items=items,
        raw_text=str(outputs)[:500],
        debug_info={"log": debug_log, "output_keys": list(outputs.keys()) if isinstance(outputs, dict) else []},
    )


# ══════════════════════════════════════════════
# 工作流 3: 纠纷多模态裁判
# PRD 4.9: 纠纷描述+商品图片 → AI 判责 → 自动结算
# ══════════════════════════════════════════════


async def judge_dispute(
    description: str,
    image_urls: list[str],
    *,
    trace_id: Optional[str] = None,
) -> DisputeJudgeOutput:
    """
    纠纷多模态裁判 — AI 核验证据并给出判责方案。

    PRD 4.9 主流程:
      1. 拼友提交纠纷 (描述 + 1-3张商品问题实拍图)
      2. 纠纷描述+图片传入 Dify 多模态模型
      3. AI 输出判责结果: 团长全责 / 拼友全责 / 双方按比例
      4. 置信度 < 80% 或金额 ≥ 500元 → 不自动执行, 转人工
      5. 系统根据判责比例自动执行退款/结算

    Args:
        description: 纠纷描述 (拼友填写的问题说明)
        image_urls:  商品问题图片 URL 列表 (1-3张)
        trace_id:    全链路追踪ID

    Returns:
        DisputeJudgeOutput 判责方案

    Raises:
        AppException: AI_DISPUTE_JUDGE_FAILED / AI_LOW_CONFIDENCE / AI_TIMEOUT
    """
    if not description or not description.strip():
        raise AppException(
            code=ErrorCode.AI_DISPUTE_JUDGE_FAILED,
            message="纠纷描述为空",
        )
    if not image_urls:
        raise AppException(
            code=ErrorCode.AI_DISPUTE_JUDGE_FAILED,
            message="纠纷证据图片为空 — 至少上传1张 (PRD 4.9)",
        )

    client = get_dify_client()
    workflow = settings.DIFY_WORKFLOW_DISPUTE
    threshold = settings.DISPUTE_JUDGE_CONFIDENCE_THRESHOLD  # 80%

    logger.info(
        f"纠纷裁判: desc_len={len(description)} images={len(image_urls)} "
        f"workflow={workflow}"
    )

    outputs = await client.run_workflow(
        workflow_name=workflow,
        inputs={
            "description": description.strip(),
            "image_urls": image_urls,
        },
        trace_id=trace_id,
    )

    confidence = float(outputs.get("confidence", 0))

    try:
        result = DisputeJudgeOutput(
            responsible_party=outputs.get("responsible_party", "shared"),
            creator_ratio=float(outputs.get("creator_ratio", 0.5)),
            participant_ratio=float(outputs.get("participant_ratio", 0.5)),
            reason=outputs.get("reason", ""),
            confidence=confidence,
        )
    except (ValueError, TypeError) as e:
        raise AppException(
            code=ErrorCode.AI_DISPUTE_JUDGE_FAILED,
            message=f"判责输出字段校验失败: {e}",
        )

    # 校验责任比例合计为 1.0 (±0.01 容差)
    total_ratio = result.creator_ratio + result.participant_ratio
    if abs(total_ratio - 1.0) > 0.01:
        raise AppException(
            code=ErrorCode.AI_DISPUTE_JUDGE_FAILED,
            message=f"判责比例合计异常: creator={result.creator_ratio} "
            f"+ participant={result.participant_ratio} = {total_ratio} (应为1.0)",
        )

    # 低置信度标记 (由调用方决定是否自动执行)
    if confidence < threshold:
        logger.warning(
            f"判责置信度不足: confidence={confidence} threshold={threshold} "
            f"→ 转人工审核 (PRD 4.9)"
        )

    logger.info(
        f"纠纷裁判完成: party={result.responsible_party} "
        f"creator={result.creator_ratio} participant={result.participant_ratio} "
        f"confidence={confidence}"
    )
    return result
