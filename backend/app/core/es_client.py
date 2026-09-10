"""
ElasticSearch 8 异步客户端封装
==============================
对应PRD 4.3 LBS雷达大厅:
  - geo_distance 1.5km 地理查询
  - 全文检索 (商品名称/描述)
  - 多条件筛选 (品类/时间/状态/现货)
  - 距离排序 + 信用分加权 + 现货前置

对应底座设计 3.1-3.5:
  - 索引: order_index
  - 同步: RabbitMQ 异步 → ES upsert
  - 容错: Consumer重试+死信队列+全量对齐
  - 一致性: MySQL为主, ES为检索副本
"""

import logging
from typing import Any, Optional

from elasticsearch import AsyncElasticsearch, BadRequestError, NotFoundError

from app.config import settings

logger = logging.getLogger(__name__)

# 全局 ES 客户端
_es_client: Optional[AsyncElasticsearch] = None

# 订单索引名称 (底座设计 3.1)
ORDER_INDEX = settings.ES_INDEX_ORDER

# ──────────────────────────────────────────────
# 订单索引 Mapping 定义 (底座设计 3.2)
# ──────────────────────────────────────────────

ORDER_INDEX_MAPPING: dict[str, Any] = {
    "settings": {
        "number_of_shards": 3,
        "number_of_replicas": 1,
        "refresh_interval": "5s",
        "max_result_window": 10000,
    },
    "mappings": {
        "properties": {
            "order_id": {"type": "long"},
            "order_no": {"type": "keyword"},
            "creator_id": {"type": "long"},
            "status": {"type": "integer"},
            "order_type": {"type": "integer"},
            "goods_name": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword"}},
                "analyzer": "ik_max_word",
                "search_analyzer": "ik_smart",
            },
            "goods_desc": {"type": "text", "analyzer": "ik_max_word"},
            "goods_category": {"type": "keyword"},
            "price_per_person": {"type": "scaled_float", "scaling_factor": 100},
            "total_people": {"type": "integer"},
            "current_people": {"type": "integer"},
            "location": {"type": "geo_point"},
            "location_text": {"type": "text"},
            "deadline": {"type": "date"},
            "credit_score": {"type": "integer"},
            "is_spot": {"type": "boolean"},
            "spot_valid_until": {"type": "date"},
            "created_at": {"type": "date"},
        }
    },
}


# ──────────────────────────────────────────────
# 初始化与关闭
# ──────────────────────────────────────────────

async def init_es() -> None:
    """初始化 ES 客户端。"""
    global _es_client
    password = settings.ES_PASSWORD.get_secret_value()
    _es_client = AsyncElasticsearch(
        hosts=[settings.ES_HOST],
        basic_auth=(settings.ES_USER, password) if password else None,
        request_timeout=settings.ES_REQUEST_TIMEOUT,
        max_retries=3,
        retry_on_timeout=True,
    )
    # 验证连接
    info = await _es_client.info()
    logger.info(f"ES 连接成功: {info['version']['number']}")


async def close_es() -> None:
    """关闭 ES 客户端。"""
    global _es_client
    if _es_client:
        await _es_client.close()
        _es_client = None


def get_es() -> AsyncElasticsearch:
    """获取 ES 客户端实例。"""
    if _es_client is None:
        raise RuntimeError("ElasticSearch 未初始化，请先调用 init_es()")
    return _es_client


# ──────────────────────────────────────────────
# 索引管理
# ──────────────────────────────────────────────

async def ensure_order_index() -> bool:
    """
    确保 order_index 存在，不存在则创建。
    幂等操作，可重复调用。
    """
    es = get_es()
    try:
        exists = await es.indices.exists(index=ORDER_INDEX)
        if not exists:
            await es.indices.create(index=ORDER_INDEX, body=ORDER_INDEX_MAPPING)
            logger.info(f"索引 {ORDER_INDEX} 创建成功（3分片/1副本/5s刷新）")
        return True
    except Exception as e:
        logger.error(f"创建索引 {ORDER_INDEX} 失败: {e}")
        return False


async def delete_order_index() -> bool:
    """删除 order_index（仅测试用）。"""
    es = get_es()
    try:
        await es.indices.delete(index=ORDER_INDEX, ignore_unavailable=True)
        return True
    except Exception as e:
        logger.error(f"删除索引 {ORDER_INDEX} 失败: {e}")
        return False


# ──────────────────────────────────────────────
# 文档 CRUD
# ──────────────────────────────────────────────

async def upsert_order_doc(order_id: int, doc: dict[str, Any], refresh: bool = False) -> bool:
    """
    创建或更新订单文档 (PRD 4.3: 订单变更时 ES 同步)。

    由 RabbitMQ order.status.sync 消费者调用 (refresh=False)。
    订单创建时直接同步调用 refresh=True，确保立即可搜索。
    """
    es = get_es()
    try:
        await es.index(
            index=ORDER_INDEX,
            id=str(order_id),
            body=doc,
            refresh=refresh,
        )
        return True
    except Exception as e:
        logger.error(f"ES upsert 订单 {order_id} 失败: {e}")
        return False


async def delete_order_doc(order_id: int) -> bool:
    """删除订单文档（订单解散/下架时调用）。"""
    es = get_es()
    try:
        await es.delete(
            index=ORDER_INDEX,
            id=str(order_id),
            ignore=[404],
        )
        return True
    except Exception as e:
        logger.error(f"ES 删除订单 {order_id} 失败: {e}")
        return False


async def get_order_doc(order_id: int) -> Optional[dict[str, Any]]:
    """获取单个订单文档。"""
    es = get_es()
    try:
        result = await es.get(index=ORDER_INDEX, id=str(order_id))
        return result["_source"]
    except NotFoundError:
        return None
    except Exception as e:
        logger.error(f"ES 查询订单 {order_id} 失败: {e}")
        return None


# ──────────────────────────────────────────────
# ⚠️ DEPRECATED: LBS 地理空间搜索 — 请使用 radar_service.search_nearby_orders
#
# 此函数硬编码 status=0 (仅 GATHERING)，不支持现货订单。
# 雷达 API 已迁移至 app.services.radar_service.search_nearby_orders。
# 保留仅供参考，请勿在新代码中调用。
# ──────────────────────────────────────────────

async def search_nearby_orders(
    lat: float,
    lng: float,
    radius_km: float = 1.5,
    current_user_id: Optional[int] = None,
    goods_category: Optional[str] = None,
    keyword: Optional[str] = None,
    filter_spot_only: bool = False,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    import warnings
    warnings.warn("search_nearby_orders 已废弃，请使用 radar_service.search_nearby_orders", DeprecationWarning, stacklevel=2)
    """
    LBS 雷达大厅核心查询。

    对应底座设计 3.3 DSL:
      - geo_distance 过滤 1.5km 范围
      - must_not: creator_id 排除本人订单 (PRD 3.1/4.3)
      - must: status=0 (仅GATHERING) + deadline未过期
      - should: is_spot 加权 / goods_name 全文搜索 / goods_category 筛选
      - sort: 距离升序 > 信用分降序 > 现货前置

    Args:
        lat: 用户纬度
        lng: 用户经度
        radius_km: 搜索半径(km)，默认1.5
        current_user_id: 当前用户ID（强制过滤本人的订单）
        goods_category: 品类筛选（可选）
        keyword: 商品名称全文搜索（可选）
        filter_spot_only: 仅看现货 (PRD 4.8)
        page: 页码
        page_size: 每页数量

    Returns:
        {"total": int, "hits": list[dict], "page": int, "page_size": int}
    """
    es = get_es()

    # 构建 bool query
    must_clauses: list[dict] = [
        {"term": {"status": 0}},  # 仅 GATHERING 状态
        {"range": {"deadline": {"gte": "now"}}},  # 未过期
    ]

    must_not_clauses: list[dict] = []
    if current_user_id is not None:
        # PRD 3.1: 强制过滤本人创建的拼单
        must_not_clauses.append({"term": {"creator_id": current_user_id}})

    filter_clauses: list[dict] = [
        {
            "geo_distance": {
                "distance": f"{radius_km}km",
                "location": {"lat": lat, "lon": lng},
            }
        }
    ]

    if filter_spot_only:
        filter_clauses.append({"term": {"is_spot": True}})

    # 可选筛选 + 搜索
    should_clauses: list[dict] = []

    if keyword:
        should_clauses.append({"match": {"goods_name": {"query": keyword, "boost": 2.0}}})
        should_clauses.append({"match": {"goods_desc": {"query": keyword, "boost": 1.0}}})

    if goods_category:
        filter_clauses.append({"term": {"goods_category": goods_category}})

    # 现货加权前置
    should_clauses.append({"term": {"is_spot": {"value": True, "boost": 1.5}}})

    query_body: dict[str, Any] = {
        "query": {
            "bool": {
                "must": must_clauses,
                "must_not": must_not_clauses,
                "filter": filter_clauses,
                **({"should": should_clauses, "minimum_should_match": 0} if should_clauses else {}),
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
            {"credit_score": {"order": "desc"}},
            {"is_spot": {"order": "desc"}},
            {"created_at": {"order": "desc"}},
        ],
        "from": (page - 1) * page_size,
        "size": page_size,
        "track_total_hits": min(10000, page * page_size * 5),  # 跟踪总数上限
    }

    try:
        result = await es.search(index=ORDER_INDEX, body=query_body)
        total = (
            result["hits"]["total"]["value"]
            if isinstance(result["hits"]["total"], dict)
            else result["hits"]["total"]
        )
        hits = []
        for hit in result["hits"]["hits"]:
            source = hit["_source"]
            # 注入排序中的距离值
            if hit.get("sort") and len(hit["sort"]) > 0:
                source["_distance_m"] = round(hit["sort"][0], 0)
            hits.append(source)

        return {
            "total": total,
            "hits": hits,
            "page": page,
            "page_size": page_size,
        }
    except Exception as e:
        logger.error(f"ES LBS搜索失败 (lat={lat}, lng={lng}): {e}")
        return {"total": 0, "hits": [], "page": page, "page_size": page_size}


# ──────────────────────────────────────────────
# 通用搜索方法
# ──────────────────────────────────────────────

async def search_orders(
    query: dict[str, Any],
    page: int = 1,
    page_size: int = 20,
    sort: Optional[list[dict]] = None,
) -> dict[str, Any]:
    """
    通用订单搜索。

    适用于后台管理、订单历史查询等非LBS场景。
    """
    es = get_es()
    body: dict[str, Any] = {
        "query": query,
        "from": (page - 1) * page_size,
        "size": page_size,
    }
    if sort:
        body["sort"] = sort

    try:
        result = await es.search(index=ORDER_INDEX, body=body)
        total = (
            result["hits"]["total"]["value"]
            if isinstance(result["hits"]["total"], dict)
            else result["hits"]["total"]
        )
        hits = [hit["_source"] for hit in result["hits"]["hits"]]
        return {"total": total, "hits": hits, "page": page, "page_size": page_size}
    except Exception as e:
        logger.error(f"ES 通用搜索失败: {e}")
        return {"total": 0, "hits": [], "page": page, "page_size": page_size}
