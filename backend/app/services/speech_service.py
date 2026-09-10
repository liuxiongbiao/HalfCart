"""
语音识别服务 — 三通道架构 (PRD 4.7)
====================================
- 阿里云 NLS 一句话识别 (主力, 中文准确率 95%+)
- Whisper tiny 本地推理 (云服务不可用时兜底)
- Web Speech API (前端 Chrome/Safari 实时识别, 无需后端)

路由策略:
  1. 前端 Web Speech → 直接给文本 NLP (不经过此服务)
  2. 后端上传音频 → 优先阿里云 NLS → 失败降级 Whisper
  3. 未配置云服务 → 直接使用 Whisper
"""

import base64
import hashlib
import hmac
import io
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from pydantic import BaseModel, Field

from app.config import settings
from app.middleware.exception_handler import AppException
from app.schemas.common import ErrorCode

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
# Whisper 懒加载 (避免启动时加载模型)
# ══════════════════════════════════════════════

_whisper_model = None


def _get_whisper_model():
    """懒加载 Whisper 模型 (首次调用时加载到内存)。"""
    global _whisper_model
    if _whisper_model is None:
        try:
            import whisper
            model_name = settings.WHISPER_MODEL or "tiny"
            logger.info(f"加载 Whisper 模型: {model_name}...")
            _whisper_model = whisper.load_model(model_name, download_root="/tmp/whisper")
            logger.info(f"Whisper 模型 {model_name} 加载完成")
        except ImportError:
            logger.warning("openai-whisper 未安装, Whisper 通道不可用")
            return None
        except Exception as e:
            logger.error(f"Whisper 模型加载失败: {e}")
            return None
    return _whisper_model


# ══════════════════════════════════════════════
# 阿里云 NLS 一句话识别 (REST API)
# ══════════════════════════════════════════════


def _aliyun_nls_token() -> str:
    """
    生成阿里云 NLS 临时 Token (有效期 24h, 生产环境应缓存)。
    文档: https://help.aliyun.com/document_detail/323549.html
    """
    access_key_id = settings.ALIYUN_NLS_ACCESS_KEY_ID
    access_key_secret = settings.ALIYUN_NLS_ACCESS_KEY_SECRET

    if not access_key_id or not access_key_secret:
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message="未配置阿里云 AccessKey, 请设置 ALIYUN_NLS_ACCESS_KEY_ID / ALIYUN_NLS_ACCESS_KEY_SECRET",
        )

    # 构造 Token 请求参数
    expire_time = int(time.time()) + 3600  # 1小时有效期
    token_json = json.dumps({
        "accessKeyId": access_key_id,
        "expireTime": expire_time,
        "userId": "halfcart",
    })

    # HMAC-SHA1 签名
    signature = hmac.new(
        access_key_secret.encode("utf-8"),
        token_json.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    signature_base64 = base64.b64encode(signature).decode("utf-8")

    return f"{token_json}.{signature_base64}"


async def aliyun_nls_asr(audio_bytes: bytes, audio_format: str = "wav") -> str:
    """
    阿里云一句话识别 (REST API)。
    文档: https://help.aliyun.com/document_detail/323554.html

    Args:
        audio_bytes: 音频数据 (≤60秒, ≤2MB)
        audio_format: wav / opus / pcm

    Returns: 识别文本
    """
    app_key = settings.ALIYUN_NLS_APP_KEY
    if not app_key:
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message="未配置阿里云 NLS AppKey",
        )

    token = _aliyun_nls_token()
    audio_base64 = base64.b64encode(audio_bytes).decode("utf-8")

    request_body = {
        "app_key": app_key,
        "format": audio_format,
        "sample_rate": 16000,
        "enable_intermediate_result": False,
        "enable_punctuation_prediction": True,
        "enable_inverse_text_normalization": True,
    }

    url = "https://nls-gateway.cn-shanghai.aliyuncs.com/rest/v1/asr"
    headers = {
        "X-NLS-Token": token,
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        # 按阿里云 NLS REST API 规范：token 在 header, 音频在 body
        try:
            resp = await client.post(url, headers=headers, json={
                **request_body,
                "payload": audio_base64,
            })
            resp.raise_for_status()
            result = resp.json()

            if result.get("status") == 20000000:
                text = result.get("result", "")
                logger.info(f"阿里云 NLS 识别成功: text_len={len(text)}")
                return text
            else:
                error_msg = result.get("status_text", "未知错误")
                logger.warning(f"阿里云 NLS 识别失败: {error_msg}")
                raise AppException(
                    code=ErrorCode.UNKNOWN_ERROR,
                    message=f"语音识别失败: {error_msg}",
                )

        except httpx.HTTPError as e:
            logger.error(f"阿里云 NLS 请求异常: {e}")
            raise AppException(
                code=ErrorCode.UNKNOWN_ERROR,
                message=f"语音服务请求异常: {str(e)[:100]}",
            )


# ══════════════════════════════════════════════
# Whisper 本地推理 (兜底)
# ══════════════════════════════════════════════


async def whisper_asr(audio_bytes: bytes) -> str:
    """
    Whisper tiny 本地语音转写 (CPU 推理, 2-5秒)。

    Args:
        audio_bytes: 音频数据 (WAV/MP3/OGG, Whisper 自动检测)

    Returns: 识别文本
    """
    model = _get_whisper_model()
    if model is None:
        raise AppException(
            code=ErrorCode.UNKNOWN_ERROR,
            message="Whisper 模型未安装或加载失败, 无法兜底识别",
        )

    try:
        # Whisper 接受 file path / bytes / numpy array
        import tempfile
        import os

        # 写入临时文件 (Whisper 对 bytes 支持不稳定, 用文件路径最可靠)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name

        try:
            result = model.transcribe(
                tmp_path,
                language="zh",
                fp16=False,  # CPU 推理
                task="transcribe",
            )
            text = result.get("text", "").strip()
            logger.info(f"Whisper 识别成功: text_len={len(text)}")

            # 中文比例检测: 过少则拒绝 (Whisper tiny 幻觉/噪音)
            import re
            chinese_chars = len(re.findall(r'[一-鿿]', text))
            if chinese_chars < max(len(text.replace(' ', '')) // 2, 2):
                logger.warning(f"Whisper 输出疑似非中文, 丢弃: {text[:80]}")
                raise AppException(
                    code=ErrorCode.PARAM_VALIDATION_ERROR,
                    message="未识别到有效中文语音，请靠近麦克风、放慢语速重试",
                )

            return text
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        logger.error(f"Whisper 识别异常: {e}")
        raise AppException(
            code=ErrorCode.UNKNOWN_ERROR,
            message=f"本地语音识别失败: {str(e)[:100]}",
        )


# ══════════════════════════════════════════════
# 统一入口 + 路由
# ══════════════════════════════════════════════


class SpeechToTextResult(BaseModel):
    """语音转文本结果。"""
    text: str = Field(..., description="识别文本")
    engine: str = Field(..., description="使用的引擎: aliyun / whisper / webspeech")
    confidence: float = Field(default=1.0, description="置信度 0-1")
    duration_ms: int = Field(default=0, description="处理耗时(毫秒)")


async def speech_to_text(
    audio_bytes: bytes,
    audio_format: str = "wav",
    preferred_engine: Optional[str] = None,
) -> SpeechToTextResult:
    """
    语音转文本 — 自动路由。

    策略:
      1. preferred_engine="webspeech" → 已在前端处理, 后端不调用
      2. 已配置阿里云 → 优先使用 → 失败降级 Whisper
      3. 未配置阿里云 → 直接使用 Whisper
    """
    start = time.time()
    engine = preferred_engine or settings.SPEECH_ENGINE or "auto"

    # ── 路由决策 ──
    use_aliyun = engine in ("auto", "aliyun") and settings.ALIYUN_NLS_APP_KEY

    text = ""
    used_engine = "whisper"

    if use_aliyun:
        try:
            text = await aliyun_nls_asr(audio_bytes, audio_format)
            used_engine = "aliyun"
        except Exception as e:
            logger.warning(f"阿里云 NLS 失败, 降级 Whisper: {e}")
            # 降级到 Whisper
            text = await whisper_asr(audio_bytes)
    else:
        text = await whisper_asr(audio_bytes)

    elapsed_ms = int((time.time() - start) * 1000)
    logger.info(f"语音识别完成: engine={used_engine} text_len={len(text)} time={elapsed_ms}ms")

    return SpeechToTextResult(
        text=text,
        engine=used_engine,
        confidence=0.95 if used_engine == "aliyun" else 0.85,
        duration_ms=elapsed_ms,
    )


async def speech_text_to_order(
    text: str,
    user_id: int,
    trace_id: Optional[str] = None,
) -> dict:
    """
    Web Speech 文本直传 → NLP → 创建订单 (跳过语音识别)。

    Chrome/Safari 浏览器使用 Web Speech API 在前端实时识别后,
    直接传文本给此函数, 无需经过 ASR 环节。
    """
    from app.services import deepseek_nlp, order_service
    from app.core.database import async_session_factory
    from decimal import Decimal
    from datetime import datetime as dt

    if not text or len(text.strip()) < 2:
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message="语音识别文本太短，请重新录制",
        )

    # NLP 解析 (DeepSeek)
    nlp_result = await deepseek_nlp.parse_order_from_text(
        text=text.strip(),
        trace_id=trace_id,
    )

    # 创建订单
    async with async_session_factory() as session:
        deadline = None
        if nlp_result.deadline:
            try:
                deadline = dt.fromisoformat(nlp_result.deadline)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        order = await order_service.create_normal_order(
            session=session,
            user_id=user_id,
            goods_name=nlp_result.goods_name,
            goods_desc=nlp_result.goods_desc or "",
            goods_category=nlp_result.goods_category or "",
            total_people=nlp_result.total_people or 2,
            price_per_person=Decimal(str(nlp_result.price_per_person or 0)),
            meet_location=nlp_result.meet_location or "",
            deadline=deadline,
            trace_id=trace_id,
        )

        return {
            "order_id": order.id,
            "order_no": order.order_no,
            "status": order.status,
            "speech_text": text,
            "speech_engine": "webspeech",
            "nlp_confidence": nlp_result.confidence,
            "auto_fill": nlp_result.auto_fill,
        }


async def speech_to_order(
    audio_bytes: bytes,
    user_id: int,
    audio_format: str = "wav",
    trace_id: Optional[str] = None,
) -> dict:
    """
    语音 → 文本 → NLP → 订单 (端到端)。

    流程:
      1. speech_to_text → 转写文本
      2. dify_service.parse_order_from_text → NLP 结构化
      3. order_service.create_normal_order → 创建订单

    返回: 同 POST /orders 的响应格式
    """
    from app.services import deepseek_nlp, order_service
    from app.core.database import async_session_factory

    # Step 1: 语音转文本
    stt_result = await speech_to_text(audio_bytes, audio_format)
    if not stt_result.text or len(stt_result.text.strip()) < 2:
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message="未识别到有效语音内容, 请重新录制",
        )

    # Step 2: NLP 解析 (DeepSeek)
    nlp_result = await deepseek_nlp.parse_order_from_text(
        text=stt_result.text.strip(),
        trace_id=trace_id,
    )

    # Step 3: 创建订单
    from decimal import Decimal
    from datetime import datetime as dt

    async with async_session_factory() as session:
        deadline = None
        if nlp_result.deadline:
            try:
                deadline = dt.fromisoformat(nlp_result.deadline)
                if deadline.tzinfo is None:
                    deadline = deadline.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        order = await order_service.create_normal_order(
            session=session,
            user_id=user_id,
            goods_name=nlp_result.goods_name,
            goods_desc=nlp_result.goods_desc or "",
            goods_category=nlp_result.goods_category or "",
            total_people=nlp_result.total_people or 2,
            price_per_person=Decimal(str(nlp_result.price_per_person or 0)),
            meet_location=nlp_result.meet_location or "",
            deadline=deadline,
            trace_id=trace_id,
        )

        return {
            "order_id": order.id,
            "order_no": order.order_no,
            "status": order.status,
            "speech_text": stt_result.text,
            "speech_engine": stt_result.engine,
            "nlp_confidence": nlp_result.confidence,
            "auto_fill": nlp_result.auto_fill,
        }
