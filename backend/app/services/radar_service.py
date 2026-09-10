"""
LBS 雷达大厅服务
================
PRD 4.3: 基于 ElasticSearch geo_distance 查询附近拼单, Redis 缓存加速。
底座设计 3.3: ES DSL 参考实现, 字段严格对齐 order_index。

核心逻辑:
  1. 构建 bool 组合查询: 过滤本人 + 过滤过期 + 区分普通/现货
  2. geo_distance 范围过滤
  3. 排序: 距离升序 > 现货前置 > 信用分降序
  4. Redis 缓存 (相同坐标+筛选, 10s TTL)
"""

import hashlib
import json
import logging
from typing import Any, Optional

from app.config import settings
from app.core.es_client import ORDER_INDEX, get_es
from app.core.redis_client import get_redis
from app.middleware.exception_handler import AppException
from app.schemas.common import ErrorCode
from app.utils.constants import OrderStatus

logger = logging.getLogger(__name__)

# 最大搜索半径 (PRD 4.3: 不超过5km)
MAX_RADIUS_KM = 5.0
# Redis 缓存 TTL (PRD 4.3防刷: 10秒内不重复查ES)
CACHE_TTL = settings.LBS_CACHE_TTL_SECONDS


# ══════════════════════════════════════════════
# 雷达列表查询
# ══════════════════════════════════════════════


async def search_nearby_orders(
    user_id: int,
    lat: float,
    lng: float,
    *,
    category: Optional[str] = None,
    keyword: Optional[str] = None,
    spot_only: bool = False,
    radius_km: float = 1.5,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    """
    LBS 雷达大厅核心查询 (PRD 4.3 主流程)。

    强制过滤:
      - creator_id != user_id     (屏蔽本人订单)
      - deadline >= now           (未过期)
      - 普通: status=0            (仅集资中)
      - 现货: status=2 AND is_spot=true AND spot_valid_until >= now

    排序:
      1. 距离由近到远 (_geo_distance asc)
      2. 现货优先 (is_spot desc)
      3. 团长信用分降序 (credit_score desc)

    Args:
        user_id:    当前用户ID (过滤本人订单)
        lat/lng:    用户坐标
        category:   品类筛选
        keyword:    全文搜索关键词
        spot_only:  仅看现货
        radius_km:  搜索半径 (0.1~5.0km)
        page:       页码
        page_size:  每页数量

    Returns:
        {"total": int, "hits": list[dict], "page": int, "page_size": int}
    """
    # ── 参数校验 ──────────────────────────
    _validate_coords(lat, lng)
    radius_km = min(max(radius_km, 0.1), MAX_RADIUS_KM)

    # ── Redis 缓存 (仅缓存第1页, 高频场景) ─
    if page == 1:
        cache_key = _build_cache_key(user_id, lat, lng, category, keyword, spot_only, radius_km)
        cached = await _get_cache(cache_key)
        if cached is not None:
            logger.debug(f"雷达缓存命中: key={cache_key[:16]}...")
            return cached

    # ── 构建 ES DSL ───────────────────────
    query_body = _build_es_dsl(
        user_id=user_id,
        lat=lat,
        lng=lng,
        category=category,
        keyword=keyword,
        spot_only=spot_only,
        radius_km=radius_km,
        page=page,
        page_size=page_size,
    )

    # ── 执行 ES 查询 ──────────────────────
    es = get_es()
    try:
        result = await es.search(index=ORDER_INDEX, body=query_body)
        parsed = _parse_es_result(result, page, page_size)
    except Exception as e:
        logger.error(f"ES 雷达查询失败 (lat={lat}, lng={lng}): {e}")
        raise AppException(
            code=ErrorCode.INTERNAL_ERROR,
            message="附近拼单查询暂时不可用，请稍后重试",
        )

    # ── 写入缓存 ──────────────────────────
    if page == 1:
        await _set_cache(cache_key, parsed)

    return parsed


# ══════════════════════════════════════════════
# ES DSL 构建 (底座设计 3.3)
# ══════════════════════════════════════════════


def _build_es_dsl(
    user_id: int,
    lat: float,
    lng: float,
    category: Optional[str],
    keyword: Optional[str],
    spot_only: bool,
    radius_km: float,
    page: int,
    page_size: int,
) -> dict:
    """构建 ES bool 查询 DSL, 字段严格对齐 order_index。"""

    # ── 状态过滤 ──────────────────────────
    if spot_only:
        # 仅现货: status=2 AND is_spot=true AND 未过期
        status_clause = {
            "bool": {
                "must": [
                    {"term": {"status": OrderStatus.DELIVERING}},
                    {"term": {"is_spot": True}},
                    {"range": {"spot_valid_until": {"gte": "now"}}},
                ]
            }
        }
    else:
        # 普通拼单 (status=0) + 现货 (status=2) 混合展示
        status_clause = {
            "bool": {
                "should": [
                    # 普通拼单: 集资中
                    {"bool": {"must": [
                        {"term": {"status": OrderStatus.GATHERING}},
                        {"term": {"order_type": 1}},
                    ]}},
                    # 现货: 待交接 + 未过期
                    {"bool": {"must": [
                        {"term": {"status": OrderStatus.DELIVERING}},
                        {"term": {"is_spot": True}},
                        {"range": {"spot_valid_until": {"gte": "now"}}},
                    ]}},
                ],
                "minimum_should_match": 1,
            }
        }

    # ── must ──────────────────────────────
    must_clauses = [
        status_clause,
        {"range": {"deadline": {"gte": "now"}}},  # 未过期
    ]

    # ── must_not ──────────────────────────
    must_not_clauses = [
        {"term": {"creator_id": user_id}},  # 屏蔽本人订单 (PRD 3.1)
    ]

    # ── filter ────────────────────────────
    filter_clauses = [
        {
            "geo_distance": {
                "distance": f"{radius_km}km",
                "location": {"lat": lat, "lon": lng},
            }
        }
    ]
    if category:
        filter_clauses.append({"term": {"goods_category": category}})

    # ── should (可选权重) ─────────────────
    should_clauses = [
        {"term": {"is_spot": {"value": True, "boost": 1.5}}},  # 现货加权
    ]
    if keyword:
        should_clauses.append({"match": {"goods_name": {"query": keyword, "boost": 2.0}}})
        should_clauses.append({"match": {"goods_desc": {"query": keyword, "boost": 1.0}}})

    return {
        "query": {
            "bool": {
                "must": must_clauses,
                "must_not": must_not_clauses,
                "filter": filter_clauses,
                "should": should_clauses,
                "minimum_should_match": 0,
            }
        },
        "sort": [
            {
                "_geo_distance": {
                    "location": {"lat": lat, "lon": lng},
                    "order": "asc",
                    "unit": "m",
                    "mode": "min",
                }
            },
            {"is_spot": {"order": "desc"}},
            {"credit_score": {"order": "desc"}},
        ],
        "from": (page - 1) * page_size,
        "size": page_size,
        "track_total_hits": min(10000, page * page_size * 10),
    }


# ══════════════════════════════════════════════
# ES 结果解析
# ══════════════════════════════════════════════


def _parse_es_result(result: dict, page: int, page_size: int) -> dict[str, Any]:
    """将 ES raw response 解析为统一格式。"""
    hits_raw = result["hits"]["hits"]
    total = (
        result["hits"]["total"]["value"]
        if isinstance(result["hits"]["total"], dict)
        else result["hits"]["total"]
    )

    hits = []
    for hit in hits_raw:
        src = hit["_source"]
        # 提取排序中的距离 (米) — _geo_distance sort 的第一个元素
        if hit.get("sort") and len(hit["sort"]) > 0:
            src["_distance_m"] = round(hit["sort"][0], 1)
        else:
            src["_distance_m"] = 0.0
        hits.append(src)

    return {"total": total, "hits": hits, "page": page, "page_size": page_size}


# ══════════════════════════════════════════════
# Redis 缓存
# ══════════════════════════════════════════════


def _build_cache_key(
    user_id: int, lat: float, lng: float,
    category: Optional[str], keyword: Optional[str],
    spot_only: bool, radius_km: float,
) -> str:
    """生成缓存键: SHA256(坐标+参数) → 去重。"""
    # 坐标精度降低到3位小数 (约100m), 提高缓存命中率
    raw = f"{user_id}:{lat:.3f}:{lng:.3f}:{category or ''}:{keyword or ''}:{spot_only}:{radius_km:.1f}"
    return f"halfcart:radar:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


async def _get_cache(key: str) -> Optional[dict]:
    """从 Redis 读取缓存。"""
    try:
        r = get_redis()
        data = await r.get(key)
        if data:
            return json.loads(data)
    except Exception:
        pass
    return None


async def _set_cache(key: str, data: dict) -> None:
    """写入 Redis 缓存 (10s TTL, PRD 4.3 防刷规则)。"""
    try:
        r = get_redis()
        await r.set(key, json.dumps(data, default=str), ex=CACHE_TTL)
    except Exception:
        pass


# ══════════════════════════════════════════════
# 工具
# ══════════════════════════════════════════════


def _validate_coords(lat: float, lng: float) -> None:
    """经纬度合法性校验 (PRD 4.3)。"""
    if not (-90 <= lat <= 90):
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message=f"纬度非法: {lat}, 范围 -90~90",
        )
    if not (-180 <= lng <= 180):
        raise AppException(
            code=ErrorCode.PARAM_VALIDATION_ERROR,
            message=f"经度非法: {lng}, 范围 -180~180",
        )
