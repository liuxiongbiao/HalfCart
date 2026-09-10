"""
DeepSeek NLP 解析服务 (替代 Dify)
==================================
将自然语言文本 → 结构化订单参数，直接调用 DeepSeek API。

相比 Dify 的优势:
  - 无需额外部署任何服务
  - API 价格极低 (￥1/百万token)
  - OpenAI 兼容接口, 代码简单
"""

import json
import logging
import re
from decimal import Decimal
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from app.config import settings
from app.middleware.exception_handler import AppException
from app.schemas.common import ErrorCode

logger = logging.getLogger(__name__)


class ASROrderOutput(BaseModel):
    """语音发单 NLP 解析输出 (PRD 4.7)。"""
    goods_name: str = Field(..., description="商品名称")
    total_people: int = Field(..., description="需要人数 ≥2")
    price_per_person: Decimal = Field(..., description="人均价格")
    deadline: str = Field(..., description="截单时间")
    meet_location: str = Field(..., description="交接地点")
    goods_desc: str = Field(default="", description="商品描述")
    goods_category: str = Field(default="", description="商品品类")
    purchase_channel: str = Field(default="", description="采购渠道")
    confidence: float = Field(default=0.0, description="置信度 0-1")
    auto_fill: bool = Field(default=True, description="是否可自动填充表单")


# ══════════════════════════════════════════════
# 核心：DeepSeek API 调用
# ══════════════════════════════════════════════

SYSTEM_PROMPT = """你是一个拼单助手。从用户的语音转写文本中提取结构化订单信息。

返回严格的 JSON（不要markdown包裹），格式：
{
  "goods_name": "商品名称",
  "total_people": 3,
  "price_per_person": 25.00,
  "deadline": "2026-07-23T12:00:00",
  "meet_location": "交接地点",
  "goods_desc": "补充描述",
  "goods_category": "品类(美食/生鲜/日用/零食/饮品)",
  "purchase_channel": "采购渠道(如山姆/盒马/菜场)",
  "confidence": 0.9
}

规则：
- total_people 必须≥2
- price_per_person 是每人价格(元)
- deadline 用 ISO8601 格式，提到"明天"="今天日期+1天"，"下午3点"=15:00:00，"中午"=12:00:00
- 未提及的字段用空字符串或0
- confidence 表示你对提取结果的把握 0-1
- 无法提取足够信息时 goods_name=""且confidence<0.5"""


async def parse_order_from_text(
    text: str,
    *,
    trace_id: Optional[str] = None,
) -> ASROrderOutput:
    """
    语音文本 → DeepSeek NLP → 结构化订单参数。

    调用 DeepSeek Chat Completions API (OpenAI 兼容)。
    """
    if not text or not text.strip():
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message="语音转写文本为空，无法解析",
        )

    api_key = settings.DEEPSEEK_API_KEY.get_secret_value()
    if not api_key:
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message="未配置 DEEPSEEK_API_KEY，请在 .env 中设置 (注册地址: https://platform.deepseek.com)",
        )

    model = settings.DEEPSEEK_MODEL
    api_base = settings.DEEPSEEK_API_BASE.rstrip("/")

    logger.info(f"DeepSeek NLP: text_len={len(text)} model={model}")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": text.strip()},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 500,
                },
            )
            resp.raise_for_status()
            data = resp.json()

    except httpx.TimeoutException:
        raise AppException(
            code=ErrorCode.UNKNOWN_ERROR,
            message="DeepSeek API 超时, 请重试",
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"DeepSeek API HTTP {e.response.status_code}: {e.response.text[:200]}")
        if e.response.status_code == 401:
            raise AppException(
                code=ErrorCode.PARAM_VALIDATION_ERROR,
                message="DeepSeek API Key 无效, 请检查 DEEPSEEK_API_KEY",
            )
        raise AppException(
            code=ErrorCode.UNKNOWN_ERROR,
            message=f"DeepSeek API 异常 (HTTP {e.response.status_code})",
        )
    except Exception as e:
        logger.error(f"DeepSeek API 请求失败: {e}")
        raise AppException(
            code=ErrorCode.UNKNOWN_ERROR,
            message=f"NLP 解析服务异常: {str(e)[:100]}",
        )

    # 提取响应文本
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    logger.info(f"DeepSeek 原始输出: {content[:200]}")

    # 解析 JSON (处理可能的 markdown 包裹)
    outputs = _parse_json_response(content)

    # 后处理
    return _validate_and_build(outputs, text)


def _parse_json_response(content: str) -> dict:
    """从 LLM 响应中提取 JSON，兼容 markdown 包裹格式。"""
    # 尝试直接解析
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # 尝试从 ```json ... ``` 中提取
    match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试从 { ... } 中提取
    match = re.search(r'\{.*\}', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    raise AppException(
        code=ErrorCode.UNKNOWN_ERROR,
        message=f"DeepSeek 返回格式异常, 无法提取 JSON: {content[:200]}",
    )


def _validate_and_build(outputs: dict, original_text: str) -> ASROrderOutput:
    """字段校验 + 后处理修正。"""
    from datetime import datetime, timedelta, timezone

    # 提取原始字段
    goods_name = str(outputs.get("goods_name", "")).strip()
    total_people = int(outputs.get("total_people", 0) or 0)
    price_per_person = float(outputs.get("price_per_person", 0) or 0)
    deadline_str = str(outputs.get("deadline", "")).strip()
    meet_location = str(outputs.get("meet_location", "")).strip()
    goods_desc = str(outputs.get("goods_desc", "")).strip()
    goods_category = str(outputs.get("goods_category", "")).strip()
    purchase_channel = str(outputs.get("purchase_channel", "")).strip()
    confidence = float(outputs.get("confidence", 0.5) or 0.5)

    # ── 后处理修正 ──

    # 人数<2 自动修正为2
    if total_people < 2:
        total_people = 2

    # 价格<=0 降置信度
    if price_per_person <= 0:
        confidence = min(confidence, 0.3)

    # deadline 智能解析 → 统一转为 UTC 时区 ISO8601
    if deadline_str:
        try:
            dt = datetime.fromisoformat(deadline_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            deadline_str = dt.isoformat()
        except (ValueError, TypeError):
            deadline_str = _parse_fuzzy_datetime(deadline_str) or deadline_str
    else:
        dt = datetime.now(timezone.utc) + timedelta(days=1)
        dt = dt.replace(hour=12, minute=0, second=0, microsecond=0)
        deadline_str = dt.isoformat()
        confidence = min(confidence, 0.6)

    # 地址未提取 → 降置信度
    if not meet_location:
        confidence = min(confidence, 0.4)

    # ── 必填字段校验 ──
    missing = []
    if not goods_name:
        missing.append("商品名称")
    if not meet_location:
        missing.append("交接地点")

    if missing:
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message=f"信息不完整, 缺少: {', '.join(missing)}",
        )

    # 确定是否自动填充
    auto_fill = confidence >= 0.6 and bool(goods_name) and bool(meet_location)

    logger.info(
        f"DeepSeek NLP 完成: goods={goods_name} people={total_people} "
        f"price={price_per_person} location={meet_location} confidence={confidence}"
    )

    return ASROrderOutput(
        goods_name=goods_name,
        total_people=total_people,
        price_per_person=Decimal(str(price_per_person)),
        deadline=deadline_str,
        meet_location=meet_location,
        goods_desc=goods_desc,
        goods_category=goods_category,
        purchase_channel=purchase_channel,
        confidence=confidence,
        auto_fill=auto_fill,
    )


def _parse_fuzzy_datetime(s: str) -> Optional[str]:
    """模糊日期解析，返回 UTC 时区 ISO8601 字符串。"""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    target = now

    s = s.strip()

    if "明天" in s: target = now + timedelta(days=1)
    elif "后天" in s: target = now + timedelta(days=2)
    elif "大后天" in s: target = now + timedelta(days=3)
    elif "今天" in s: pass  # keep now
    else: target = now + timedelta(days=1)  # default: tomorrow

    hour, minute = 12, 0
    time_match = re.search(r'(\d{1,2})[点:：](\d{0,2})?', s)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2)) if time_match.group(2) else 0
    elif "早上" in s or "上午" in s: hour, minute = 9, 0
    elif "中午" in s: hour, minute = 12, 0
    elif "下午" in s: hour = 15 if hour == 12 else min(hour + 12, 23) if hour < 12 else hour
    elif "晚上" in s: hour = 19 if hour == 12 else max(hour, 18)

    target = target.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if target <= now:
        target += timedelta(days=1)

    return target.isoformat()
