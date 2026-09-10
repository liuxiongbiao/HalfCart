"""
Redis 异步客户端封装与分布式锁工具
==================================
对应PRD:
- 4.3 LBS雷达: GEO查询缓存、防刷窗口
- 4.5 核销结算: 分布式锁防并发
- 4.1 登录: 短信验证码存储
- 5.1 接口限流: 滑动窗口

使用:
    from app.core.redis_client import get_redis, RedisLock
    r = get_redis()
    await r.set("key", "value", ex=60)
    async with RedisLock("lock:order:123") as locked:
        if locked:
            ...
"""

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

# 全局 Redis 连接池实例
_redis_pool: Optional[Redis] = None


async def init_redis() -> None:
    """
    初始化 Redis 连接池。
    PRD 5.1: 全局共享连接池，避免频繁创建连接。
    """
    global _redis_pool
    _redis_pool = aioredis.from_url(
        settings.REDIS_URL,
        max_connections=settings.REDIS_POOL_SIZE,
        socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
        decode_responses=True,  # 自动将 bytes 解码为 str
        health_check_interval=30,
    )
    # 验证连接
    await _redis_pool.ping()


async def close_redis() -> None:
    """关闭 Redis 连接池。"""
    global _redis_pool
    if _redis_pool:
        await _redis_pool.close()
        _redis_pool = None


def get_redis() -> Redis:
    """
    获取 Redis 客户端实例（供依赖注入和业务层使用）。
    使用前必须确保已调用 init_redis()。
    """
    if _redis_pool is None:
        raise RuntimeError("Redis 未初始化，请先调用 init_redis()")
    return _redis_pool


# ══════════════════════════════════════════════
# 分布式锁工具类
# PRD 5.1: Redis 分布式锁 + MySQL SELECT FOR UPDATE 行级锁双重防护
# 用于加入拼单、核销验证等并发核心接口
# ══════════════════════════════════════════════

class RedisLock:
    """
    Redis 分布式锁，支持上下文管理器。

    特性:
    - 基于 SETNX + 过期时间实现
    - 自动续期（看门狗机制）
    - 释放时通过 Lua 脚本校验持有者，防误删
    - 支持 await 超时等待

    用法:
        async with RedisLock("lock:order:join:123", timeout=10) as acquired:
            if acquired:
                # 执行并发安全操作
                ...
    """

    _RENEW_INTERVAL = 0.5  # 续期间隔（秒），即锁有效期的 1/2
    _DEFAULT_LOCK_TIMEOUT = 10  # 默认锁超时（秒）

    # Lua 脚本：仅当 value 匹配时释放锁
    _LUA_RELEASE_SCRIPT = """
    if redis.call("GET", KEYS[1]) == ARGV[1] then
        return redis.call("DEL", KEYS[1])
    else
        return 0
    end
    """

    def __init__(
        self,
        lock_key: str,
        timeout: int = _DEFAULT_LOCK_TIMEOUT,
        auto_renew: bool = True,
    ):
        """
        初始化分布式锁。

        Args:
            lock_key: 锁键名（建议格式: "lock:<业务场景>:<资源ID>"）
            timeout: 锁超时时间（秒），超时后自动释放防止死锁
            auto_renew: 是否启用看门狗自动续期
        """
        self._lock_key = f"halfcart:{lock_key}"
        self._lock_value: str = ""
        self._timeout = timeout
        self._auto_renew = auto_renew
        self._renew_task: Optional[asyncio.Task] = None
        self._redis: Optional[Redis] = None

    async def __aenter__(self) -> bool:
        """尝试获取锁，返回 True 表示获取成功。"""
        self._redis = get_redis()
        self._lock_value = f"{uuid.uuid4().hex}:{id(self)}"

        # SET key value NX EX timeout
        acquired = await self._redis.set(
            self._lock_key,
            self._lock_value,
            nx=True,
            ex=self._timeout,
        )

        if acquired and self._auto_renew:
            self._start_renew()

        return bool(acquired)

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """释放锁。"""
        if self._renew_task:
            self._renew_task.cancel()
            self._renew_task = None

        if self._redis and self._lock_value:
            try:
                # Lua 脚本原子释放，仅当 value 匹配时删除
                await self._redis.eval(
                    self._LUA_RELEASE_SCRIPT,
                    1,
                    self._lock_key,
                    self._lock_value,
                )
            except Exception as e:
                logger.warning(f"释放分布式锁失败 [{self._lock_key}]: {e}")

    def _start_renew(self) -> None:
        """启动看门狗协程，定期续期锁。"""
        async def renew_loop():
            interval = max(self._timeout // 2, 1)
            while True:
                await asyncio.sleep(interval)
                try:
                    # Lua 脚本：仅当 value 匹配时才续期
                    renew_script = """
                    if redis.call("GET", KEYS[1]) == ARGV[1] then
                        return redis.call("EXPIRE", KEYS[1], ARGV[2])
                    else
                        return 0
                    end
                    """
                    await self._redis.eval(
                        renew_script,
                        1,
                        self._lock_key,
                        self._lock_value,
                        self._timeout,
                    )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"续期分布式锁失败 [{self._lock_key}]: {e}")

        self._renew_task = asyncio.create_task(renew_loop())


# ══════════════════════════════════════════════
# Redis GEO 辅助函数 (PRD 4.3 LBS雷达)
# ══════════════════════════════════════════════

async def geo_add_members(
    key: str,
    *members: tuple[float, float, str],
    ex: Optional[int] = None,
) -> int:
    """
    添加地理位置成员到 GEO 集合。

    Args:
        key: GEO集合键名
        members: (经度, 纬度, 成员ID) 的可变元组
        ex: 过期时间(秒)

    Returns:
        新增成员数
    """
    r = get_redis()
    added = await r.geoadd(key, members)
    if ex:
        await r.expire(key, ex)
    return added


async def geo_search_nearby(
    key: str,
    longitude: float,
    latitude: float,
    radius_km: float = 1.5,
    unit: str = "km",
    count: Optional[int] = None,
    sort: str = "ASC",
) -> list[dict]:
    """
    GEOSEARCH 周边成员查询。

    Args:
        key: GEO集合键名
        longitude: 中心经度
        latitude: 中心纬度
        radius_km: 搜索半径(km)，默认1.5km (PRD 4.3)
        unit: 距离单位 (m/km/ft/mi)
        count: 返回数量限制
        sort: 排序方向 (ASC=由近到远)

    Returns:
        [{member: str, distance: float}, ...]
    """
    r = get_redis()
    results = await r.geosearch(
        key,
        longitude=longitude,
        latitude=latitude,
        radius=radius_km,
        unit=unit,
        count=count,
        sort=sort,
        withdist=True,
    )
    return [{"member": member, "distance": dist} for member, dist in results]


# ══════════════════════════════════════════════
# 接口限流 (PRD 4.1/5.1 滑动窗口)
# ══════════════════════════════════════════════

async def rate_limit_check(
    key: str,
    max_requests: int,
    window_seconds: int,
) -> bool:
    """
    基于Redis滑动窗口的接口限流检查。

    PRD 4.1: 验证码接口 同一手机号1分钟限1次、1小时限5次
    PRD 5.1: 核心接口防刷保护

    Args:
        key: 限流键名
        max_requests: 窗口内最大请求数
        window_seconds: 滑动窗口时间(秒)

    Returns:
        True=允许通过, False=触发限流
    """
    r = get_redis()
    now_ms = int(asyncio.get_event_loop().time() * 1000)
    window_ms = window_seconds * 1000
    clear_before = now_ms - window_ms

    # Lua 脚本原子操作：清理过期记录 + 添加新记录 + 判断
    lua_script = """
    local key = KEYS[1]
    local now = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local max_req = tonumber(ARGV[3])
    local clear_before = now - window
    redis.call("ZREMRANGEBYSCORE", key, 0, clear_before)
    local count = redis.call("ZCARD", key)
    if count >= max_req then
        return 0
    end
    redis.call("ZADD", key, now, now .. ":" .. math.random())
    redis.call("EXPIRE", key, math.ceil(window / 1000) + 1)
    return 1
    """

    result = await r.eval(
        lua_script,
        1,
        f"halfcart:ratelimit:{key}",
        now_ms,
        window_ms,
        max_requests,
    )
    return bool(result)
