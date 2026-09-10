"""
高德 LBS 增强服务
=================
PRD 4.3: 定位反欺诈、坐标校验、IP 兜底定位、地址解析适配。

职责:
  - 雷达列表前置校验 (坐标合法性 + 精度 + 位移反欺诈)
  - 发单地址 → 坐标自动转换
  - 用户历史定位记录 (供反欺诈使用)
  - 所有方法支持降级 (高德不可用时使用本地计算)

不负责:
  - 雷达核心查询 (继续由 radar_service 负责)
  - Redis GEO 管理 (由 redis_client 负责)
"""

import json
import logging
import time
from typing import Optional, Tuple

from app.config import settings
from app.core.redis_client import get_redis
from app.middleware.exception_handler import AppException
from app.schemas.common import ErrorCode
from app.utils.amap_client import get_amap

logger = logging.getLogger(__name__)

# ── Redis Key ──
_USER_LOCATION_HISTORY_KEY = "halfcart:lbs:history:{user_id}"
_FRAUD_FLAG_KEY = "halfcart:lbs:fraud_flag:{user_id}"


# ══════════════════════════════════════════════
# 雷达前置校验链 (按 PRD 4.3 顺序执行)
# ══════════════════════════════════════════════


async def check_radar_prerequisites(
    user_id: int,
    lat: float,
    lng: float,
    accuracy: float = 0,
) -> dict:
    """
    雷达列表查询前置校验 (PRD 4.3)。

    链路:
      1. 坐标合法性校验 (amap check_coord_valid)
      2. GPS 精度校验 (低于阈值降权/拒绝)
      3. 位移异常检测 (10min 内位移 > 50km → 异常)
      4. 记录用户历史定位

    Returns:
        {"passed": bool, "warning": str, "fraud": bool}
    """
    result = {"passed": True, "warning": "", "fraud": False}
    amap = get_amap()

    # Step 1: 坐标合法性
    valid, reason = await amap.check_coord_valid(lng, lat)
    if not valid:
        logger.warning(f"雷达坐标校验失败: user_id={user_id} {reason}")
        raise AppException(
            code=ErrorCode.LBS_LOCATION_DENIED,
            message=f"定位异常: {reason}",
        )

    # Step 2: GPS 精度校验
    if accuracy > 0 and accuracy > settings.AMAP_FRAUD_ACCURACY_THRESHOLD:
        result["warning"] = f"定位精度较低 ({accuracy:.0f}m), 请前往开阔地带获得更准确的位置"
        result["fraud"] = True
        logger.info(f"雷达低精度定位: user_id={user_id} accuracy={accuracy:.0f}m")

    # Step 3: 位移异常检测
    fraud = await _check_movement_fraud(user_id, lat, lng)
    if fraud:
        result["warning"] = "检测到定位异常, 请联系客服"
        result["fraud"] = True
        result["passed"] = False
        logger.warning(f"雷达位移异常: user_id={user_id}")
        raise AppException(
            code=ErrorCode.LBS_LOCATION_ANOMALY,
            message="定位出现异常跳动, 请确认使用真实位置后重试",
        )

    # Step 4: 记录历史定位
    await _record_location(user_id, lat, lng)

    return result


# ══════════════════════════════════════════════
# 发单地址 → 坐标转换
# ══════════════════════════════════════════════


async def geocode_address(address: str, city: str = "") -> dict:
    """
    发单时地址文本 → 经纬度转换。

    与订单一起入库, 失败时返回 None 由调用方处理。
    """
    amap = get_amap()
    result = await amap.geocode(address, city)
    if not result:
        raise AppException(
            code=ErrorCode.LBS_NO_RESULTS,
            message=f"地址解析失败: {address}。请补充更详细的地址信息, 如小区名称、道路名称",
        )
    logger.info(f"地址解析成功: {address[:20]}... → ({result['lat']}, {result['lng']})")
    return result


# ══════════════════════════════════════════════
# 逆地理编码
# ══════════════════════════════════════════════


async def regeo_location(lng: float, lat: float) -> Optional[dict]:
    """坐标 → 可读地址。用于订单详情展示格式化地址。"""
    amap = get_amap()
    return await amap.regeo(lng, lat)


# ══════════════════════════════════════════════
# IP 兜底定位
# ══════════════════════════════════════════════


async def ip_fallback_location(ip: str) -> Optional[dict]:
    """
    用户未传坐标时的 IP 兜底定位。

    返回坐标级数据, 精度较低 (城市级)。
    """
    amap = get_amap()
    result = await amap.ip_locate(ip)
    if result:
        logger.info(f"IP兜底定位: ip={ip[:8]}... → city={result.get('city','')}")
    return result


# ══════════════════════════════════════════════
# POI 搜索
# ══════════════════════════════════════════════


async def search_poi(
    keyword: str,
    city: str = "",
    lng: Optional[float] = None,
    lat: Optional[float] = None,
) -> list[dict]:
    """POI 关键字搜索, 供前端发单页选择交接地址。"""
    amap = get_amap()
    return await amap.poi_search(keyword, city, lng, lat)


# ══════════════════════════════════════════════
# 反欺诈 — 位移异常检测
# ══════════════════════════════════════════════


async def _check_movement_fraud(user_id: int, lat: float, lng: float) -> bool:
    """
    位移异常检测 (PRD 4.3 反欺诈)。

    规则: 对比 10 分钟内最近的定位记录,
          位移速度 > AMAP_FRAUD_SPEED_LIMIT (默认 300km/h) → 判定为瞬移异常。

    返回 True 表示异常。
    """
    r = get_redis()
    history_key = _USER_LOCATION_HISTORY_KEY.format(user_id=user_id)

    try:
        raw = await r.lindex(history_key, 0)  # 最新一条
        if not raw:
            return False

        record = json.loads(raw)
        prev_lat = record.get("lat", 0)
        prev_lng = record.get("lng", 0)
        prev_ts = record.get("ts", 0)

        elapsed_sec = time.time() - prev_ts
        if elapsed_sec <= 0 or elapsed_sec > 600:  # 只检查 10 分钟内的记录
            return False

        # Haversine 距离
        distance_km = _haversine_km(prev_lat, prev_lng, lat, lng)
        speed_kmh = distance_km / (elapsed_sec / 3600)

        if speed_kmh > settings.AMAP_FRAUD_SPEED_LIMIT:
            logger.warning(
                f"位移异常: user_id={user_id} "
                f"distance={distance_km:.2f}km time={elapsed_sec:.0f}s "
                f"speed={speed_kmh:.0f}km/h (阈值={settings.AMAP_FRAUD_SPEED_LIMIT})"
            )
            return True

    except Exception as e:
        logger.warning(f"位移检测异常: {e}")

    return False


async def _record_location(user_id: int, lat: float, lng: float) -> None:
    """记录用户最近定位 (Redis List, 保留最近 10 条)。"""
    r = get_redis()
    history_key = _USER_LOCATION_HISTORY_KEY.format(user_id=user_id)
    try:
        record = json.dumps({"lat": lat, "lng": lng, "ts": time.time()})
        await r.lpush(history_key, record)
        await r.ltrim(history_key, 0, 9)  # 保留最近10条
        await r.expire(history_key, 3600)  # 1小时过期
    except Exception:
        pass


# ══════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════


import math as _math


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Haversine 距离 (公里)。"""
    r = 6371.0
    phi1, phi2 = _math.radians(lat1), _math.radians(lat2)
    dphi = _math.radians(lat2 - lat1)
    dlam = _math.radians(lng2 - lng1)
    a = _math.sin(dphi / 2) ** 2 + _math.cos(phi1) * _math.cos(phi2) * _math.sin(dlam / 2) ** 2
    return r * 2 * _math.atan2(_math.sqrt(a), _math.sqrt(1 - a))
