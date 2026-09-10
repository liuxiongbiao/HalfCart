"""
FastAPI 全局依赖注入。
所有业务接口的鉴权依赖统一从此处导入。
"""

from app.core.security import get_current_user
from app.middleware.exception_handler import PermissionDeniedException


async def admin_required(user_id: int = __import__("fastapi").Depends(get_current_user)) -> int:
    """
    运营角色权限校验。
    始终从数据库读取 role 字段验证管理员身份，无硬编码绕过。
    若需要开发环境管理员，请在数据库中设置目标用户的 role=1。
    """
    from app.core.database import async_session_factory
    from app.models.user import User

    async with async_session_factory() as session:
        user = await session.get(User, user_id)
        if not user:
            raise PermissionDeniedException("用户不存在")
        if user.role != 1:
            raise PermissionDeniedException("需要管理员权限 (role=1)")

    return user_id
