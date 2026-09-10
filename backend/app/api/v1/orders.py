"""
订单管理 API — /api/v1/orders/*
===============================
PRD 4.2/4.4: 订单CRUD + 状态流转, 需鉴权。

接口:
  POST   /                        创建普通拼单
  GET    /{order_id}              订单详情
  GET    /my/created              我发起的订单
  GET    /my/joined               我参与的订单
  POST   /{order_id}/mark-purchased 团长标记已采购 (1→2)
  POST   /{order_id}/disband      解散订单 (0→4)
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.api.deps import get_current_user
from app.core.database import get_db
from app.middleware.exception_handler import AppException
from app.schemas.common import APIResponse, ErrorCode, PaginationResponse
from app.schemas.verification import VerifyRequest
from app.services import order_service
from app.services import speech_service
from app.utils.constants import OrderStatus

router = APIRouter()


async def _validate_goods_category(category: str) -> None:
    """校验 goods_category 必须在 categories 表中存在 (除非为空)。"""
    if not category or not category.strip():
        return  # 空值允许 (向后兼容)
    from app.core.database import async_session_factory
    from app.models.category import Category
    from sqlalchemy import select

    async with async_session_factory() as session:
        result = await session.execute(
            select(Category).where(Category.key == category, Category.is_active == 1)
        )
        if not result.scalar_one_or_none():
            raise AppException(
                code=ErrorCode.PARAM_VALIDATION_ERROR,
                message=f"品类 '{category}' 不在可选范围内",
            )


# ══════════════════════════════════════════════
# 请求 Schema
# ══════════════════════════════════════════════


class CreateNormalOrderRequest(BaseModel):
    """创建普通拼单请求 (PRD 4.7: AI解析后确认或手动填写)。"""

    goods_name: str = Field(..., min_length=1, max_length=100, description="商品名称")
    goods_desc: str = Field(default="", max_length=500, description="商品描述")
    goods_category: str = Field(default="", max_length=30, description="商品品类")
    purchase_channel: str = Field(default="", max_length=50, description="采购渠道 如山姆")
    total_people: int = Field(default=2, ge=2, description="需要总人数, 含团长")
    price_per_person: str = Field(..., description="人均价格 (Decimal字符串)")
    meet_location: str = Field(..., min_length=1, max_length=200, description="交接地点")
    location_lat: str = Field(..., description="纬度")
    location_lng: str = Field(..., description="经度")
    deadline: Optional[str] = Field(default=None, description="截单时间 (ISO8601)")

    @model_validator(mode="after")
    def check_deadline(self):
        if self.deadline:
            dt = datetime.fromisoformat(self.deadline)
            if dt <= datetime.now():
                raise ValueError("截单时间必须晚于当前时间")
        return self


class CreateSpotOrderRequest(BaseModel):
    """创建现货转让订单请求 (PRD 4.8)。"""

    goods_name: str = Field(..., min_length=1, max_length=100, description="商品名称")
    transfer_price: float = Field(..., gt=0, description="转让价格")
    meet_location: str = Field(default="", max_length=200, description="交接地点")
    location_lat: float = Field(default=0.0, description="纬度")
    location_lng: float = Field(default=0.0, description="经度")
    receipt_img: str = Field(default="", description="小票图片(base64或URL)")
    original_price: Optional[float] = Field(default=None, description="小票原价")
    goods_category: str = Field(default="", max_length=30, description="商品品类")
    goods_desc: str = Field(default="", max_length=500, description="商品描述")


# ══════════════════════════════════════════════
# 响应辅助
# ══════════════════════════════════════════════


def _order_to_dict(order) -> dict:
    """Order ORM → 前端友好 dict。"""
    return {
        "order_id": order.id,
        "order_no": order.order_no,
        "creator_id": order.creator_id,
        "status": order.status,
        "status_label": OrderStatus.label(order.status),
        "order_type": order.order_type,
        "goods_name": order.goods_name,
        "goods_desc": order.goods_desc,
        "goods_category": order.goods_category,
        "purchase_channel": order.purchase_channel,
        "total_people": order.total_people,
        "current_people": order.current_people,
        "price_per_person": str(order.price_per_person),
        "total_amount": str(order.total_amount),
        "meet_location": order.meet_location,
        "location_lat": str(order.location_lat),
        "location_lng": str(order.location_lng),
        "deadline": order.deadline.isoformat() if order.deadline else None,
        "is_spot": bool(order.is_spot),
        "dispute_status": order.dispute_status,
        "finished_at": order.finished_at.isoformat() if order.finished_at else None,
        "created_at": order.created_at.isoformat() if order.created_at else None,
    }


# ══════════════════════════════════════════════
# 接口
# ══════════════════════════════════════════════


@router.post("", summary="创建普通拼单订单")
async def create_normal_order(
    req: CreateNormalOrderRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """PRD 4.2/4.7: 创建 GATHERING(0) 订单, 团长自动为首个参与人。"""
    await _validate_goods_category(req.goods_category)
    order = await order_service.create_normal_order(
        session=db,
        user_id=user_id,
        goods_name=req.goods_name,
        goods_desc=req.goods_desc,
        goods_category=req.goods_category,
        purchase_channel=req.purchase_channel,
        total_people=req.total_people,
        price_per_person=Decimal(req.price_per_person),
        meet_location=req.meet_location,
        location_lat=Decimal(req.location_lat),
        location_lng=Decimal(req.location_lng),
        deadline=datetime.fromisoformat(req.deadline) if req.deadline else None,
    )
    return APIResponse.success(data=_order_to_dict(order), message="拼单创建成功")


@router.post("/spot", summary="创建现货转让订单")
async def create_spot_order(
    req: CreateSpotOrderRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """PRD 4.8: 创建 DELIVERING(2) 现货订单, is_spot=1。"""
    await _validate_goods_category(req.goods_category)
    order = await order_service.create_spot_order(
        session=db,
        user_id=user_id,
        goods_name=req.goods_name,
        transfer_price=Decimal(str(req.transfer_price)),
        meet_location=req.meet_location,
        location_lat=Decimal(str(req.location_lat)),
        location_lng=Decimal(str(req.location_lng)),
        receipt_img=req.receipt_img[:255],
        original_price=Decimal(str(req.original_price)) if req.original_price else None,
        goods_category=req.goods_category,
        goods_desc=req.goods_desc,
    )
    return APIResponse.success(data=_order_to_dict(order), message="现货订单创建成功")


@router.post("/{order_id}/buy-spot", summary="购买现货 (PRD 4.8)")
async def buy_spot_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """
    PRD 4.8: 拼友在雷达大厅看到现货订单, 支付后直接获取核销码, 线下自提。

    流程: 校验现货+DELIVERING → 扣钱包 → 冻结团长 → 参与人 → 返回核销码
    """
    from app.services import participation_service as ps
    result = await ps.join_spot_order(db, user_id, order_id)

    return APIResponse.success(
        data={
            "order_id": result["order"].id,
            "status": result["order"].status,
            "verify_code": result.get("verify_code", ""),
        },
        message="购买成功, 核销码已生成, 请线下出示给团长完成取货",
    )


@router.get("/discover", summary="发现页推荐Feed")
async def discover_feed(
    keyword: Optional[str] = Query(default=None, description="搜索关键词"),
    user_id: int = Depends(get_current_user),
):
    """
    发现页推荐 (方案B: 热门/高信用/最新三个板块)。

    从 ES 查询三段数据:
      - hot:     参团人数最多的 TOP 10
      - trusted: 高信用团长 (credit ≥ 90) 最新发单 TOP 10
      - latest:  全平台最新 20 条

    全部过滤: 已过期 + 本人订单
    """
    from app.core.es_client import ORDER_INDEX, get_es, ensure_order_index

    await ensure_order_index()
    es = get_es()

    # ── 共用过滤: 排除自己 + 未过期 ──
    base_filters = [
        {"range": {"deadline": {"gte": "now"}}},
    ]
    base_must_not = [
        {"term": {"creator_id": user_id}},
    ]

    async def _query(sort: list, size: int) -> list[dict]:
        body = {
            "query": {
                "bool": {
                    "filter": base_filters,
                    "must_not": base_must_not,
                }
            },
            "sort": sort,
            "size": size,
        }
        if keyword and keyword.strip():
            body["query"]["bool"]["should"] = [
                {"match": {"goods_name": {"query": keyword.strip(), "boost": 2.0}}},
                {"match": {"goods_desc": {"query": keyword.strip(), "boost": 1.0}}},
            ]
            body["query"]["bool"]["minimum_should_match"] = 1
        try:
            result = await es.search(index=ORDER_INDEX, body=body)
            return [h["_source"] for h in result["hits"]["hits"]]
        except Exception:
            return []

    try:
        hot = await _query([{"current_people": {"order": "desc"}}], 10)
        trusted = await _query(
            [{"credit_score": {"order": "desc"}}, {"created_at": {"order": "desc"}}], 10
        )
        latest = await _query([{"created_at": {"order": "desc"}}], 20)
    except Exception as e:
        return APIResponse.error(code=ErrorCode.INTERNAL_ERROR, message=f"推荐数据查询失败: {e}")

    return APIResponse.success(
        data={
            "hot": [_hit_to_card(h) for h in hot],
            "trusted": [_hit_to_card(h) for h in trusted],
            "latest": [_hit_to_card(h) for h in latest],
        },
        message="ok",
    )


def _hit_to_card(hit: dict) -> dict:
    """ES hit → 前端订单卡片格式。"""
    total = hit.get("total_people", 0)
    current = hit.get("current_people", 0)
    return {
        "orderId": hit.get("order_id", 0),
        "orderNo": hit.get("order_no", ""),
        "goodsName": hit.get("goods_name", ""),
        "goodsCategory": hit.get("goods_category", ""),
        "pricePerPerson": str(hit.get("price_per_person", 0)),
        "totalPeople": total,
        "currentPeople": current,
        "remainingPeople": max(0, total - current),
        "meetLocation": hit.get("location_text", "") or "",
        "isSpot": hit.get("is_spot", False),
        "deadline": hit.get("deadline"),
        "creditScore": hit.get("credit_score", 100),
        "status": hit.get("status", 0),
        "statusLabel": "",
        "creatorNickname": "",
    }


@router.get("/{order_id}", summary="订单详情")
async def get_order_detail(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """PRD 4.4: 订单详情 + 参与人 + 当前用户角色。"""
    detail = await order_service.get_order_detail(db, order_id, user_id)

    # PRD 4.4/4.5: 核销码仅对订单参与人（团长/已支付拼友）可见
    from sqlalchemy import select
    from app.models.verification_code import VerificationCode
    user_role = detail["user_role"]
    verify_code = ""
    if user_role is not None:  # 仅参与人可查看核销码
        vc_result = await db.execute(
            select(VerificationCode).where(VerificationCode.order_id == order_id)
        )
        vc = vc_result.scalar_one_or_none()
        verify_code = vc.code if vc and not vc.is_used else ""

    return APIResponse.success(
        data={
            "order": _order_to_dict(detail["order"]),
            "participants": [
                {
                    "user_id": p.user_id,
                    "role": p.role,
                    "pay_status": p.pay_status,
                    "pay_amount": str(p.pay_amount),
                }
                for p in detail["participants"]
            ],
            "user_role": detail["user_role"],
            "verify_code": verify_code,
        }
    )


@router.get("/my/created", summary="我发起的订单")
async def my_created_orders(
    status: Optional[int] = Query(default=None, description="状态筛选"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """PRD 3.1 个人中心: 发起的历史订单。"""
    orders, total = await order_service.list_my_created_orders(
        db, user_id=user_id, status=status, page=page, page_size=page_size
    )
    return APIResponse.success(
        data=PaginationResponse(
            total=total,
            page=page,
            page_size=page_size,
            items=[_order_to_dict(o) for o in orders],
        ).model_dump()
    )


@router.get("/my/joined", summary="我参与的订单")
async def my_joined_orders(
    status: Optional[int] = Query(default=None, description="状态筛选"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """PRD 3.1 个人中心: 参与的历史订单 (排除本人发起的)。"""
    orders, total = await order_service.list_my_joined_orders(
        db, user_id=user_id, status=status, page=page, page_size=page_size
    )
    return APIResponse.success(
        data=PaginationResponse(
            total=total,
            page=page,
            page_size=page_size,
            items=[_order_to_dict(o) for o in orders],
        ).model_dump()
    )


@router.post("/{order_id}/join", summary="加入拼单")
async def join_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """
    PRD 4.4 拼友视角: 加入拼单 — 扣钱包余额 → 冻结至团长账户。

    仅 GATHERING 普通拼单可加入, 团长不可加入自己订单, 已参与者幂等返回。
    """
    from app.services import participation_service

    result = await participation_service.join_order(db, user_id, order_id)
    order = result["order"]

    resp = {
        "order_id": order.id,
        "status": order.status,
        "status_label": OrderStatus.label(order.status),
        "current_people": order.current_people,
        "total_people": order.total_people,
        "remaining_slots": max(0, order.total_people - order.current_people),
        "deducted_amount": str(order.price_per_person) if not result.get("already_joined") else "0",
        "wallet_balance_after": str(result["wallet_after"]) if result["wallet_after"] else None,
        "auto_transitioned": result.get("transitioned", False),
        "already_joined": result.get("already_joined", False),
    }

    msg = "已加入拼单"
    if result.get("already_joined"):
        msg = "你已参与该拼单 (幂等)"
    elif result.get("transitioned"):
        msg = "已加入拼单 — 人数已满, 自动进入采购阶段"

    return APIResponse.success(data=resp, message=msg)


@router.post("/{order_id}/quit", summary="退出拼单")
async def quit_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """
    PRD 4.6: 退出拼单 — GATHERING 全额退款, frozen→wallet。

    仅 GATHERING 状态允许退出, 团长不可退出, 已退出者幂等返回。
    """
    from app.services import participation_service

    result = await participation_service.quit_order(db, user_id, order_id)
    order = result["order"]

    return APIResponse.success(
        data={
            "order_id": order.id,
            "refunded_amount": str(result["refund_amount"]),
            "wallet_balance_after": str(result["wallet_after"]),
            "current_people": order.current_people,
            "already_quit": result.get("already_quit", False),
        },
        message="已退出拼单, 金额已退回" if not result.get("already_quit") else "你已退出该拼单 (幂等)",
    )


@router.post("/{order_id}/verify", summary="核销验证 (2→3)")
async def verify_order(
    order_id: int,
    req: VerifyRequest,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """
    PRD 4.5: 团长输入6位核销码, 验证通过后冻结→钱包 (扣3%服务费), 订单完成。

    仅 DELIVERING 状态可核销, 核销码连续错误5次锁定10分钟。
    """
    from app.services import verification_service

    result = await verification_service.settle_order(db, user_id, order_id, req.verify_code)

    if result.get("already_settled"):
        return APIResponse.success(
            data={"order_id": order_id, "status": 3, "status_label": "已完成"},
            message="订单已核销 (幂等)",
        )

    return APIResponse.success(
        data={
            "order_id": order_id,
            "status": result["order"].status,
            "status_label": OrderStatus.label(result["order"].status),
            "total_amount": str(result["total_amount"]),
            "service_fee": str(result["service_fee"]),
            "income": str(result["income"]),
            "wallet_after": result["wallet_after"],
        },
        message=f"核销成功: 收入 {result['income']}, 服务费 {result['service_fee']}",
    )


@router.post("/{order_id}/refund", summary="申请退款 (阶梯)")
async def refund_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """
    PRD 4.6 阶梯退款:
      GATHERING(0) → 全额退款, 无违约金
      PURCHASING(1) → 扣20%违约金给团长, 80%退回拼友
      DELIVERING(2)+ → 禁止
    """
    from app.services import verification_service

    result = await verification_service.refund_order(db, user_id, order_id)
    order = result["order"]

    if result.get("already_refunded"):
        return APIResponse.success(
            data={"order_id": order_id, "refund_amount": str(result["refund_amount"]), "already_refunded": True},
            message="已退款 (幂等)",
        )

    return APIResponse.success(
        data={
            "order_id": order_id,
            "status": order.status,
            "status_label": OrderStatus.label(order.status),
            "refund_amount": str(result["refund_amount"]),
            "penalty_amount": str(result.get("penalty_amount", "0")),
            "refund_ratio": result["refund_ratio"],
            "wallet_after": result["wallet_after"],
        },
        message=f"退款成功: {result['refund_amount']} 已退回钱包",
    )


@router.post("/{order_id}/mark-purchased", summary="团长标记已采购 (1→2)")
async def mark_purchased(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """PRD 4.2: PURCHASING → DELIVERING, 仅团长操作。"""
    order = await order_service.transition_to_delivering(db, order_id, user_id)
    return APIResponse.success(data=_order_to_dict(order), message="已标记为待交接")


@router.post("/{order_id}/disband", summary="解散订单 (0→4)")
async def disband(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """PRD 4.2: 仅 GATHERING 状态可解散。"""
    order = await order_service.disband_order(db, order_id, user_id)
    return APIResponse.success(data=_order_to_dict(order), message="订单已解散")


@router.delete("/{order_id}", summary="删除订单")
async def delete_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
):
    """
    删除订单 (补充规则):
      - 仅订单创建者可删除
      - 待交接(DELIVERING/2) 不可删除
      - 待成团/采购中(GATHERING/0 PURCHASING/1) → 解散 + 退款
      - 已完成/已取消(FINISHED/3 DISBANDED/4) → 软删除
    """
    result = await order_service.delete_order(db, order_id, user_id)
    return APIResponse.success(message=result["message"])


class SpeechToOrderRequest(BaseModel):
    """语音发单请求 — 上传音频或直接传文本创建订单 (PRD 4.7)。"""
    audio_data: str = Field(
        default="", description="Base64 编码的音频数据 (Web Speech路径可传空)"
    )
    audio_format: str = Field(
        default="webm", description="音频格式: webm / wav / opus"
    )
    text: str = Field(
        default="", description="Web Speech 已转写的文本 (跳过语音识别)"
    )


@router.post("/speech-to-order", summary="语音发单 (PRD 4.7)")
async def speech_to_order(
    req: SpeechToOrderRequest,
    user_id: int = Depends(get_current_user),
):
    """
    PRD 4.7: 语音 → 转写 → NLP 解析 → 创建订单。

    三通道:
      - 前端 Web Speech API (Chrome/Safari 实时识别) → 传 text 字段
      - 阿里云 NLS 一句话识别 (主力) → 传 audio_data (base64)
      - Whisper tiny 本地兜底 → 传 audio_data, 阿里云不可用自动降级

    请求: { audio_data?: "<base64>", text?: "已识别的文本", audio_format?: "webm|wav" }
    响应: { order_id, speech_text, speech_engine, nlp_confidence, auto_fill }
    """
    import base64 as b64

    # 路径1: Web Speech 已转写文本
    if req.text and not req.audio_data:
        result = await speech_service.speech_text_to_order(
            text=req.text.strip(),
            user_id=user_id,
        )
        result["speech_engine"] = "webspeech"
        return APIResponse.success(data=result, message="语音发单成功 (Web Speech)")

    # 路径2+3: 音频 → ASR → NLP
    if not req.audio_data:
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message="请提供音频数据或已识别的文本",
        )

    try:
        audio_bytes = b64.b64decode(req.audio_data)
    except Exception:
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message="音频数据 Base64 解码失败",
        )

    max_size = settings.MAX_AUDIO_SIZE_BYTES
    if len(audio_bytes) > max_size:
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message=f"音频文件过大 (最大 {max_size // 1024}KB)",
        )

    result = await speech_service.speech_to_order(
        audio_bytes=audio_bytes,
        user_id=user_id,
        audio_format=req.audio_format,
    )

    return APIResponse.success(data=result, message="语音发单成功")
