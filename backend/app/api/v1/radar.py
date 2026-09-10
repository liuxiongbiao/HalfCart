"""
LBS 雷达大厅 API — /api/v1/radar/*
===================================
PRD 4.3: 附近拼单瀑布流, 含定位反欺诈前置校验。

接口:
  GET /list  雷达列表 (需鉴权, 含坐标校验+位移检测)
"""

from fastapi import APIRouter, Depends, Query
from typing import Optional

from app.api.deps import get_current_user
from app.schemas.common import APIResponse, ErrorCode
from app.schemas.radar import RadarOrderCard
from app.services import amap_service, radar_service
from app.utils.constants import OrderStatus
from app.utils.privacy import mask_address

router = APIRouter()


def _to_radar_card(hit: dict) -> RadarOrderCard:
    """将 ES hit 转换为雷达卡片。"""
    total = hit.get("total_people", 0)
    current = hit.get("current_people", 0)
    return RadarOrderCard(
        order_id=hit.get("order_id", 0),
        order_no=hit.get("order_no", ""),
        goods_name=hit.get("goods_name", ""),
        goods_category=hit.get("goods_category", ""),
        price_per_person=str(hit.get("price_per_person", 0)),
        total_people=total,
        remaining_people=max(0, total - current),
        distance_m=hit.get("_distance_m", 0),
        meet_location=mask_address(hit.get("location_text", "")),
        is_spot=hit.get("is_spot", False),
        deadline=hit.get("deadline"),
        credit_score=hit.get("credit_score", 100),
        status=hit.get("status", 0),
        status_label=OrderStatus.label(hit.get("status", 0)),
    )


@router.get("/list", summary="雷达大厅列表")
async def radar_list(
    lat: float = Query(..., ge=-90, le=90, description="用户当前纬度"),
    lng: float = Query(..., ge=-180, le=180, description="用户当前经度"),
    accuracy: float = Query(default=0, ge=0, description="GPS定位精度(米)"),
    category: Optional[str] = Query(default=None, description="商品品类筛选"),
    keyword: Optional[str] = Query(default=None, description="商品名称/描述搜索"),
    spot_only: bool = Query(default=False, description="仅看现货"),
    radius_km: float = Query(default=1.5, ge=0.1, le=5.0, description="搜索半径(km)"),
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    user_id: int = Depends(get_current_user),
):
    """
    LBS 雷达大厅 (PRD 4.3 完整版)。

    前置校验: 坐标合法性 → GPS精度 → 位移异常检测
    核心查询: ES geo_distance 1.5km + 屏蔽本人 + 距离/现货/信用分排序
    """
    # ── 前置校验 (PRD 4.3 反欺诈) ──
    try:
        prereq = await amap_service.check_radar_prerequisites(
            user_id=user_id, lat=lat, lng=lng, accuracy=accuracy,
        )
    except Exception as e:
        return APIResponse.error(
            code=ErrorCode.LBS_LOCATION_ANOMALY
            if "异常" in str(e) else ErrorCode.LBS_LOCATION_DENIED,
            message=str(e),
        )

    # ── 核心查询 ──
    result = await radar_service.search_nearby_orders(
        user_id=user_id, lat=lat, lng=lng,
        category=category, keyword=keyword, spot_only=spot_only,
        radius_km=radius_km, page=page, page_size=page_size,
    )

    cards = [_to_radar_card(hit) for hit in result["hits"]]

    return APIResponse.success(
        data={
            "total": result["total"],
            "page": result["page"],
            "page_size": result["page_size"],
            "items": [c.model_dump() for c in cards],
            "warning": prereq.get("warning", ""),
        }
    )
