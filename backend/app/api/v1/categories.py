"""
品类字典 API — /api/v1/categories
=================================
提供前端品类筛选栏的动态数据源。

接口:
  GET /  获取所有启用的品类 (无需鉴权, 有Redis缓存)
"""

import json
import logging

from fastapi import APIRouter
from sqlalchemy import select

from app.core.database import async_session_factory
from app.core.redis_client import get_redis
from app.models.category import Category
from app.schemas.common import APIResponse, ErrorCode

logger = logging.getLogger(__name__)

router = APIRouter()

# Redis 缓存 Key
_CACHE_KEY = "halfcart:categories:active"
_CACHE_TTL = 3600  # 1小时


@router.get("", summary="获取品类列表")
@router.get("/", summary="获取品类列表")
async def list_categories():
    """
    获取所有启用的商品品类, 按 sort_order 排序。

    - 无鉴权要求
    - Redis 缓存 1 小时
    - 返回 key / label / icon, 供前端分类筛选栏和发单页品类选择使用
    """
    # ── 尝试 Redis 缓存 ──────────────────────
    try:
        r = get_redis()
        cached = await r.get(_CACHE_KEY)
        if cached:
            return APIResponse.success(
                data={"categories": json.loads(cached)},
                message="ok (cached)",
            )
    except Exception:
        pass

    # ── 查询数据库 ──────────────────────────
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(Category)
                .where(Category.is_active == 1)
                .order_by(Category.sort_order)
            )
            categories = result.scalars().all()

        data = [
            {
                "key": c.key,
                "label": c.label,
                "icon": c.icon,
            }
            for c in categories
        ]

        # ── 写入缓存 ────────────────────────
        try:
            r = get_redis()
            await r.setex(_CACHE_KEY, _CACHE_TTL, json.dumps(data, ensure_ascii=False))
        except Exception:
            pass

        return APIResponse.success(
            data={"categories": data},
            message="ok",
        )

    except Exception as e:
        logger.error(f"获取品类列表失败: {e}")
        return APIResponse.error(
            code=ErrorCode.INTERNAL_ERROR,
            message="品类数据暂时不可用，请稍后重试",
        )
