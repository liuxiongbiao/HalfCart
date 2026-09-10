"""
SQLAlchemy 2.0 异步数据库引擎与会话管理
======================================
对应底座设计 2.2-2.9: 所有MySQL表的数据持久化。
PRD 5.3: 通过数据库访问白名单、应用层参数验签、全量流水对账三重保障资金数据安全。

使用:
    from app.core.database import get_db
    async for session in get_db():
        ...
"""

from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# ──────────────────────────────────────────────
# 异步引擎
# pool_size: 连接池大小，对应 docker-compose 中 DB_POOL_SIZE
# pool_recycle: 连接回收时间，避免 MySQL wait_timeout 断开
# echo: SQL日志，开发环境可开启调试
# ──────────────────────────────────────────────

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=True,   # 连接前检测可用性
    echo=settings.DB_ECHO,
    # MySQL 8.0 字符集
    connect_args={
        "charset": "utf8mb4",
    },
)

# ──────────────────────────────────────────────
# 异步会话工厂
# expire_on_commit=False: 提交后不使对象过期，方便在事务外访问
# ──────────────────────────────────────────────

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


# ──────────────────────────────────────────────
# SQLAlchemy 声明式基类
# 所有 ORM 模型继承此类
# ──────────────────────────────────────────────

class Base(DeclarativeBase):
    """ORM 基类。子类自动映射到数据库表。"""
    pass


# ──────────────────────────────────────────────
# 生命周期管理
# ──────────────────────────────────────────────

async def init_db() -> None:
    """
    初始化数据库引擎 + 自动建表 (开发环境)。
    由 main.py lifespan 启动阶段调用。
    """
    # 自动创建所有已注册的表 (幂等, 已存在则跳过)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 验证连接可用
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
        await conn.commit()


async def close_db() -> None:
    """
    关闭数据库引擎，释放所有连接。
    由 main.py lifespan 关闭阶段调用。
    """
    await engine.dispose()


# ──────────────────────────────────────────────
# 依赖注入: get_db
# FastAPI 接口层通过 Depends(get_db) 获取异步会话
# ──────────────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    获取数据库会话（FastAPI 依赖注入）。

    每请求一个会话，请求结束后自动关闭。
    用于 api 层注入：
        @router.get("/orders")
        async def list_orders(db: AsyncSession = Depends(get_db)):
            ...
    """
    async with async_session_factory() as session:
        try:
            yield session
            # 正常结束，提交事务（由调用方显式 commit）
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
