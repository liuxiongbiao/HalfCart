"""
Dify AI 工作流 API — /api/v1/dify/*
=======================================
每个 Dify 工作流对应独立路由, 互不干扰。

当前已接入:
  POST /text-parser   — 拼单文本解析 (PRD 4.7)
  POST /ocr-parser    — 小票 OCR 识别 (PRD 4.8)
  POST /dispute       — 纠纷裁判 (PRD 4.9)
"""

import logging

from fastapi import APIRouter, Depends

logger = logging.getLogger(__name__)
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.schemas.common import APIResponse, ErrorCode
from app.services.dify_text_parser import get_text_parser

router = APIRouter()


# ══════════════════════════════════════════════
# 拼单文本解析
# ══════════════════════════════════════════════


class TextParserRequest(BaseModel):
    text: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="用户输入的自然语言文本",
        examples=["山姆瑞士卷一盒 3个人拼 每人35 明天下午3点南门见"],
    )


@router.post("/text-parser", summary="拼单文本解析")
async def text_parser(
    req: TextParserRequest,
    user_id: int = Depends(get_current_user),
):
    """
    PRD 4.7: 将自然语言文本解析为结构化拼单参数。

    - 置信度 ≥ 阈值 → auto_fill=true, 前端可自动填充表单
    - 置信度 < 阈值 → auto_fill=false, 前端展示人工确认页
    - 服务异常 → auto_fill=false, 提示用户手动填写
    """
    wf = get_text_parser()
    result = await wf.run(req.text)
    return APIResponse.success(
        data=result.model_dump(),
        message="解析完成" if result.auto_fill else result.message,
    )


# ══════════════════════════════════════════════
# [新增工作流] OCR 小票识别 / 纠纷裁判
# ══════════════════════════════════════════════

# ── OCR 小票识别 (PRD 4.8) ──

class OCRParserRequest(BaseModel):
    image_url: str = Field(
        ...,
        min_length=1,
        max_length=10 * 1024 * 1024,  # 允许 base64 数据 (最大 ~10MB)
        description="小票图片URL 或 base64 data URL (data:image/jpeg;base64,...)",
    )


@router.post("/ocr-parser", summary="小票OCR识别")
async def ocr_parser(
    req: OCRParserRequest,
    user_id: int = Depends(get_current_user),
):
    """
    PRD 4.8: 从购物小票图片中提取商品列表。

    支持两种入参:
      - 图片 URL:    "https://example.com/receipt.jpg"
      - base64:      "data:image/jpeg;base64,/9j/4AAQ..."

    base64 直接透传给 Dify 工作流 (Dify LLM 节点原生支持 base64 图片输入)。
    返回识别出的所有商品，前端展示列表供用户选择转让。
    """
    from app.config import settings as _settings
    from app.services.dify_service import parse_receipt_ocr

    # 双重校验: base64 限制 (Pydantic 10MB) → 解码后二进制限制 (config MAX_IMAGE_SIZE_BYTES)
    max_bin = _settings.MAX_IMAGE_SIZE_BYTES
    if len(req.image_url) > max_bin * 2:  # base64 预留约 33% overhead
        return APIResponse.error(code=ErrorCode.PARAM_VALIDATION_ERROR, message=f"图片过大，请压缩至 {max_bin//1024//1024}MB 以内")

    try:
        result = await parse_receipt_ocr(req.image_url)
    except Exception as e:
        logger.error(f"OCR识别失败: {e}", exc_info=True)
        return APIResponse.error(code=ErrorCode.AI_DISPUTE_JUDGE_FAILED, message=f"小票识别服务暂时不可用: {str(e)[:100]}")

    items = [
        {
            "item_name": it.item_name,
            "unit_price": it.unit_price,
            "total_price": it.total_price,
            "quantity": it.quantity,
        }
        for it in result.items
    ]

    return APIResponse.success(
        data={
            "items": items,
            "count": len(items),
            "raw_text": result.raw_text,
            "debugInfo": result.debug_info,
        },
        message=f"识别到 {len(items)} 件商品",
    )


# ── 纠纷裁判 (PRD 4.9) ──

class DisputeJudgeRequest(BaseModel):
    description: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="纠纷描述",
    )
    image_urls: list[str] = Field(
        ...,
        min_length=1,
        max_length=3,
        description="商品问题实拍图URL列表(1-3张)",
    )


@router.post("/dispute", summary="AI纠纷裁判")
async def dispute_judge(
    req: DisputeJudgeRequest,
    user_id: int = Depends(get_current_user),
):
    """
    PRD 4.9: 核验纠纷证据并给出判责方案。

    - 置信度 ≥ 80% + 金额 < 500元 → 自动执行判责
    - 置信度 < 80% 或 金额 ≥ 500元 → 转人工审核
    """
    from app.services.dify_service import judge_dispute

    try:
        result = await judge_dispute(req.description, req.image_urls)
    except Exception as e:
        logger.error(f"纠纷裁判失败: {e}", exc_info=True)
        return APIResponse.error(code=ErrorCode.AI_DISPUTE_JUDGE_FAILED, message=f"AI裁判服务暂时不可用: {str(e)[:100]}")
    threshold = 0.80
    auto_execute = result.confidence >= threshold
    return APIResponse.success(
        data={
            "responsible_party": result.responsible_party,
            "creator_ratio": result.creator_ratio,
            "participant_ratio": result.participant_ratio,
            "reason": result.reason,
            "confidence": result.confidence,
            "auto_execute": auto_execute,
        },
        message="判责完成" if auto_execute else f"判责置信度较低({result.confidence:.0%}), 转人工审核",
    )
