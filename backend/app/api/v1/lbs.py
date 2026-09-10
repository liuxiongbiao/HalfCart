"""
高德 LBS 增强 API — /api/v1/lbs/*
===================================
PRD 4.3: POI搜索/地址解析/逆地理编码/IP定位兜底。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.schemas.common import APIResponse, ErrorCode
from app.services import amap_service

router = APIRouter()


# ══════════════════════════════════════════════
# Schemas
# ══════════════════════════════════════════════


class GeocodeRequest(BaseModel):
    address: str = Field(..., min_length=1, max_length=200, description="地址文本")
    city: str = Field(default="", max_length=50, description="城市(可选,限定搜索范围)")


# ══════════════════════════════════════════════
# POI 搜索
# ══════════════════════════════════════════════


@router.get("/poi/search", summary="POI关键字搜索")
async def poi_search(
    keyword: str = Query(..., min_length=1, max_length=50, description="搜索关键词"),
    city: str = Query(default="", max_length=50, description="城市(可选)"),
    lng: Optional[float] = Query(default=None, description="当前经度(可选,周边优先)"),
    lat: Optional[float] = Query(default=None, description="当前纬度(可选,周边优先)"),
    user_id: int = Depends(get_current_user),
):
    """
    POI 关键字搜索 (PRD 4.3)。

    前端发单页使用此接口搜索交接地点。
    结果带缓存, 相同关键词+坐标 24h 内不重复请求高德。
    """
    results = await amap_service.search_poi(
        keyword=keyword, city=city, lng=lng, lat=lat,
    )
    return APIResponse.success(
        data={"keyword": keyword, "total": len(results), "pois": results},
        message="查询成功",
    )


# ══════════════════════════════════════════════
# 地址 → 坐标 (地理编码)
# ══════════════════════════════════════════════


@router.post("/geocode", summary="地址解析→坐标")
async def geocode(
    req: GeocodeRequest,
    user_id: int = Depends(get_current_user),
):
    """
    地址文本 → 经纬度坐标 (PRD 4.3)。

    发单时后台自动调用, 将用户填写的地址转为坐标入库。
    解析失败时返回明确提示, 引导补充详细地址。
    """
    try:
        result = await amap_service.geocode_address(req.address, req.city)
        return APIResponse.success(data=result, message="地址解析成功")
    except Exception as e:
        return APIResponse.error(code=ErrorCode.LBS_NO_RESULTS, message=str(e))


# ══════════════════════════════════════════════
# 逆地理编码 (坐标 → 地址)
# ══════════════════════════════════════════════


@router.get("/regeo", summary="坐标→地址")
async def regeo(
    lng: float = Query(..., ge=-180, le=180, description="经度"),
    lat: float = Query(..., ge=-90, le=90, description="纬度"),
    user_id: int = Depends(get_current_user),
):
    """
    坐标 → 格式化地址 (逆地理编码)。

    用于订单详情展示当前可读地址。
    """
    result = await amap_service.regeo_location(lng, lat)
    if not result:
        return APIResponse.error(
            code=ErrorCode.LBS_NO_RESULTS,
            message="无法识别的坐标, 请确认位置信息",
        )
    return APIResponse.success(data=result, message="查询成功")


# ══════════════════════════════════════════════
# IP 兜底定位
# ══════════════════════════════════════════════


@router.get("/ip-locate", summary="IP粗略定位")
async def ip_locate(
    user_id: int = Depends(get_current_user),
):
    """
    IP 粗略定位 — 无 GPS 时的兜底方案 (PRD 4.3)。

    精度为城市级别, 仅作为备用入口。
    """
    from fastapi import Request
    from app.core.redis_client import get_redis

    try:
        result = await amap_service.ip_fallback_location("")
        if result:
            return APIResponse.success(
                data=result, message="已通过IP粗略定位, 精度较低, 建议授权GPS获得更好的体验"
            )
    except Exception:
        pass

    return APIResponse.error(
        code=ErrorCode.LBS_LOCATION_DENIED,
        message="无法获取您的位置, 请手动授权定位权限",
    )
