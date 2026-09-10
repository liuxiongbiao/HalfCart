"""
收款账户服务层 — 增删改查 + 默认账户管理
=========================================
规则:
  - 每用户最多 5 个收款账户
  - 同一用户下账户类型不得重复 (微信/支付宝各只能存一个)
  - 设默认时自动取消旧默认
  - 删除默认账户后, 若仍有其他账户, 将最近更新的设为默认
  - real_name / account 写入前 AES-256-CBC 加密
"""
import logging

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_phone
from app.models.pay_account import UserPaymentAccount
from app.schemas.common import ErrorCode

logger = logging.getLogger(__name__)

MAX_ACCOUNTS_PER_USER = 5


async def list_accounts(
    db: AsyncSession, user_id: int
) -> list[UserPaymentAccount]:
    """获取用户的所有收款账户 (默认优先)。"""
    result = await db.execute(
        select(UserPaymentAccount)
        .where(UserPaymentAccount.user_id == user_id)
        .order_by(
            UserPaymentAccount.is_default.desc(),
            UserPaymentAccount.updated_at.desc(),
        )
    )
    return list(result.scalars().all())


async def create_account(
    db: AsyncSession, user_id: int, account_type: int, real_name: str, account: str
) -> tuple[UserPaymentAccount | None, str | None]:
    """新增收款账户。返回 (account, error_message)。"""
    existing = await list_accounts(db, user_id)

    if len(existing) >= MAX_ACCOUNTS_PER_USER:
        return None, f"最多保存 {MAX_ACCOUNTS_PER_USER} 个收款账户"

    if any(a.account_type == account_type for a in existing):
        type_label = "微信" if account_type == 1 else "支付宝"
        return None, f"已存在 {type_label} 账户，请先删除旧的"

    is_default = len(existing) == 0  # 首个账户自动设为默认

    acct = UserPaymentAccount(
        user_id=user_id,
        account_type=account_type,
        real_name=encrypt_phone(real_name.strip()),
        account=encrypt_phone(account.strip()),
        is_default=1 if is_default else 0,
    )
    db.add(acct)
    await db.commit()
    await db.refresh(acct)
    logger.info(f"用户 {user_id} 新增收款账户 id={acct.id}, type={account_type}")
    return acct, None


async def update_account(
    db: AsyncSession, user_id: int, account_id: int,
    account_type: int, real_name: str, account: str,
) -> tuple[UserPaymentAccount | None, str | None]:
    """编辑收款账户。返回 (account, error_message)。"""
    acct = await db.get(UserPaymentAccount, account_id)
    if not acct or acct.user_id != user_id:
        return None, "账户不存在"

    # 检查同类型是否已被其他账户占用
    existing = await list_accounts(db, user_id)
    dup = [a for a in existing if a.account_type == account_type and a.id != account_id]
    if dup:
        type_label = "微信" if account_type == 1 else "支付宝"
        return None, f"已存在 {type_label} 账户，请先删除旧的"

    acct.account_type = account_type
    acct.real_name = encrypt_phone(real_name.strip())
    acct.account = encrypt_phone(account.strip())
    db.add(acct)
    await db.commit()
    await db.refresh(acct)
    logger.info(f"用户 {user_id} 更新收款账户 id={account_id}")
    return acct, None


async def delete_account(
    db: AsyncSession, user_id: int, account_id: int,
) -> str | None:
    """删除收款账户。返回 error_message 或 None。"""
    acct = await db.get(UserPaymentAccount, account_id)
    if not acct or acct.user_id != user_id:
        return "账户不存在"

    was_default = acct.is_default
    await db.delete(acct)
    await db.commit()

    # 如果删除的是默认账户, 将另一个设为默认
    if was_default:
        remaining = await list_accounts(db, user_id)
        if remaining:
            new_default = remaining[0]
            new_default.is_default = 1
            db.add(new_default)
            await db.commit()
            logger.info(f"删除默认账户后, id={new_default.id} 自动设为默认")

    logger.info(f"用户 {user_id} 删除收款账户 id={account_id}")
    return None


async def set_default(
    db: AsyncSession, user_id: int, account_id: int,
) -> str | None:
    """设为默认收款账户。返回 error_message 或 None。"""
    acct = await db.get(UserPaymentAccount, account_id)
    if not acct or acct.user_id != user_id:
        return "账户不存在"

    if acct.is_default:
        return None  # 已经是默认, no-op

    # 取消旧默认
    await db.execute(
        update(UserPaymentAccount)
        .where(UserPaymentAccount.user_id == user_id)
        .values(is_default=0)
    )

    acct.is_default = 1
    db.add(acct)
    await db.commit()
    logger.info(f"用户 {user_id} 设置默认收款账户 id={account_id}")
    return None
