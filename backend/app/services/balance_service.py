"""
资金原子操作服务
================
严格对齐 PRD 资金规则，所有变更遵循：
  PRD 3.2 虚拟账本: wallet_balance (可提现) + frozen_balance (冻结担保金)
  PRD 4.5 资金结算: 核销扣除 3% 服务费
  PRD 4.6 阶梯退款: 违约金直接划转团长钱包
  PRD 5.3 资金安全: 事务 + 锁 + 幂等 + 流水

设计原则:
  1. MySQL 事务 (Atomic) + Redis 分布式锁 双重防护
  2. 资金字段 DECIMAL(12,2) 明文存储，使用 Decimal 运算
  3. 每次变动同步写入 t_payment_logs，与余额变更同事务
  4. 幂等键 UNIQUE 索引兜底，重复调用只返回已有流水
  5. 余额不足时抛出 AppException，事务自动回滚
"""

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.redis_client import RedisLock, get_redis
from app.middleware.exception_handler import AppException
from app.models.payment_log import PaymentLog
from app.models.user import User
from app.schemas.common import ErrorCode
from app.utils.constants import BalanceType, PaymentLogType
from app.utils.idempotent import BusinessScene, generate_idempotent_key

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════


def _now_utc() -> datetime:
    """当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def _gen_log_no() -> str:
    """
    生成全局唯一流水单号。
    格式: HC + 时间戳(ms) + 6位随机 hex
    总长度: 2 + 13 + 6 = 21 (base26 适配 CHAR(26))
    """
    ts = str(int(_now_utc().timestamp() * 1000))  # 13位毫秒时间戳
    rand = uuid.uuid4().hex[:9]  # 9位随机
    return f"HC{ts}{rand}"


def _validate_amount(amount: Decimal, operation: str) -> None:
    """
    校验金额合法性。
    金额必须 > 0 且为两位小数正数。
    """
    if amount <= Decimal("0"):
        raise AppException(
            code=ErrorCode.PAYMENT_AMOUNT_INVALID,
            message=f"{operation}金额必须大于 0，当前值: {amount}",
        )
    if amount.as_tuple().exponent < -2:
        raise AppException(
            code=ErrorCode.PAYMENT_AMOUNT_INVALID,
            message=f"{operation}金额精度最多两位小数，当前值: {amount}",
        )


async def _insert_log_and_commit(
    session: AsyncSession,
    *,
    user_id: int,
    order_id: int | None,
    log_type: PaymentLogType,
    amount: Decimal,
    balance_before: Decimal,
    balance_after: Decimal,
    target_balance_type: BalanceType,
    idempotent_key: str,
    remark: str = "",
) -> PaymentLog:
    """
    写入资金流水记录 + 提交事务。

    同一事务内完成:
      1. 余额 UPDATE (调用方已执行)
      2. 流水 INSERT (本函数)
      3. COMMIT

    幂等保护: idempotent_key UNIQUE 索引 — 重复插入由数据库拒绝。
    """
    log = PaymentLog(
        log_no=_gen_log_no(),
        user_id=user_id,
        order_id=order_id,
        type=log_type.value,
        amount=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        target_balance_type=target_balance_type.value,
        status=1,
        remark=remark,
        idempotent_key=idempotent_key,
    )
    session.add(log)

    try:
        await session.commit()
        logger.info(
            f"资金流水已写入: log_no={log.log_no} user={user_id} "
            f"type={log_type.name} amount={amount} idempotent={idempotent_key[:12]}..."
        )
        return log
    except Exception as e:
        await session.rollback()
        logger.error(f"资金流水写入失败: {e}", exc_info=True)
        # 检查是否为幂等键冲突（重复操作）
        if "Duplicate entry" in str(e) and "idempotent_key" in str(e):
            # 幂等：重新查询已有流水返回
            existing = await _find_existing_by_idempotent(session, idempotent_key)
            if existing:
                logger.warning(f"幂等拦截: 重复资金操作 idempotent={idempotent_key[:12]}...")
                return existing
        raise AppException(
            code=ErrorCode.PAYMENT_DUPLICATE,
            message="资金操作失败，可能重复或数据库异常",
        )


async def _find_existing_by_idempotent(
    session: AsyncSession, idempotent_key: str
) -> Optional[PaymentLog]:
    """查询已存在的幂等流水 (用于幂等返回)。"""
    from sqlalchemy import select

    result = await session.execute(
        select(PaymentLog).where(PaymentLog.idempotent_key == idempotent_key)
    )
    return result.scalar_one_or_none()


def _lock_key(user_id: int, operation: str) -> str:
    """生成 Redis 分布式锁键名。"""
    return f"balance:user:{user_id}:{operation}"


# ══════════════════════════════════════════════════════════
# 操作 1: 冻结余额增加
# PRD 3.2/4.5: 拼友支付后，资金进入平台托管，体现在团长 frozen_balance
# ══════════════════════════════════════════════════════════


async def freeze_balance_increase(
    session: AsyncSession,
    *,
    user_id: int,
    order_id: int,
    amount: Decimal,
    idempotent_key: str,
    remark: str = "",
) -> PaymentLog:
    """
    冻结余额增加 — 拼友支付后，对应团长的 f frozen_balance += amount。

    PRD 3.2: 拼友支付后，资金落入平台托管，账面体现在团长的该字段下（不可提现）。
    PRD 5.3: 资金字段明文存储支持原子运算 UPDATE ... SET frozen_balance = frozen_balance + ?

    Args:
        session:  数据库事务会话
        user_id:  团长用户ID
        order_id: 订单ID
        amount:   冻结金额 (Decimal, >0)
        idempotent_key: 幂等键
        remark:   备注

    Returns:
        PaymentLog 流水记录

    Raises:
        AppException: 金额无效 / 用户不存在 / 锁冲突 / 幂等冲突
    """
    _validate_amount(amount, "冻结")

    lock = RedisLock(_lock_key(user_id, "freeze_incr"), timeout=10)
    async with lock as acquired:
        if not acquired:
            raise AppException(
                code=ErrorCode.PAYMENT_LOCK_FAILED,
                message="资金操作繁忙，请稍后重试",
            )

        # 行级锁读取用户记录 (SELECT ... FOR UPDATE)
        user = await _lock_user_row(session, user_id)

        before = user.frozen_balance
        after = before + amount

        # 原子 UPDATE (Decimal 运算)
        await session.execute(
            text(
                "UPDATE t_users SET frozen_balance = frozen_balance + :amt "
                "WHERE id = :uid"
            ),
            {"amt": amount, "uid": user_id},
        )

        return await _insert_log_and_commit(
            session,
            user_id=user_id,
            order_id=order_id,
            log_type=PaymentLogType.PAYMENT_FREEZE,
            amount=amount,
            balance_before=before,
            balance_after=after,
            target_balance_type=BalanceType.FROZEN,
            idempotent_key=idempotent_key,
            remark=remark or f"订单 {order_id} 拼友支付冻结",
        )


# ══════════════════════════════════════════════════════════
# 操作 2: 冻结余额扣减
# PRD 4.6: 退款/订单解散时扣减冻结余额
# ══════════════════════════════════════════════════════════


async def freeze_balance_decrease(
    session: AsyncSession,
    *,
    user_id: int,
    order_id: int,
    amount: Decimal,
    idempotent_key: str,
    remark: str = "",
) -> PaymentLog:
    """
    冻结余额扣减 — 退款 / 订单解散时 frozen_balance -= amount。

    PRD 4.6 阶梯退款: 状态0全额退款，状态1扣违约金后退款。
    余额不足时抛出 PAYMENT_FROZEN_INSUFFICIENT 异常。

    Args:
        session:  数据库事务会话
        user_id:  用户ID (团长，其冻结金被扣减)
        order_id: 订单ID
        amount:   扣减金额 (Decimal, >0)
        idempotent_key: 幂等键
        remark:   备注

    Returns:
        PaymentLog 流水记录

    Raises:
        AppException: PAYMENT_FROZEN_INSUFFICIENT / PAYMENT_AMOUNT_INVALID
    """
    _validate_amount(amount, "冻结扣减")

    lock = RedisLock(_lock_key(user_id, "freeze_decr"), timeout=10)
    async with lock as acquired:
        if not acquired:
            raise AppException(
                code=ErrorCode.PAYMENT_LOCK_FAILED,
                message="资金操作繁忙，请稍后重试",
            )

        user = await _lock_user_row(session, user_id)

        if user.frozen_balance < amount:
            raise AppException(
                code=ErrorCode.PAYMENT_FROZEN_INSUFFICIENT,
                message=f"冻结余额不足: 当前 {user.frozen_balance}，需要 {amount}",
            )

        before = user.frozen_balance
        after = before - amount

        await session.execute(
            text(
                "UPDATE t_users SET frozen_balance = frozen_balance - :amt "
                "WHERE id = :uid AND frozen_balance >= :amt"
            ),
            {"amt": amount, "uid": user_id},
        )

        return await _insert_log_and_commit(
            session,
            user_id=user_id,
            order_id=order_id,
            log_type=PaymentLogType.REFUND_UNFREEZE,
            amount=-amount,  # 负值表示出账
            balance_before=before,
            balance_after=after,
            target_balance_type=BalanceType.FROZEN,
            idempotent_key=idempotent_key,
            remark=remark or f"订单 {order_id} 退款解冻",
        )


# ══════════════════════════════════════════════════════════
# 操作 3: 钱包余额增加
# PRD 4.5: 核销结算/违约金补偿时 wallet_balance += amount
# ══════════════════════════════════════════════════════════


async def wallet_balance_increase(
    session: AsyncSession,
    *,
    user_id: int,
    order_id: int,
    amount: Decimal,
    log_type: PaymentLogType,
    idempotent_key: str,
    remark: str = "",
) -> PaymentLog:
    """
    钱包余额增加 — wallet_balance += amount。

    适用场景:
      - 核销结算后团长收入 (type=SETTLEMENT)
      - 违约金补偿划入团长钱包 (type=PENALTY_INCOME)
      - 提现退回 (type=WITHDRAW_REJECT)

    PRD 3.2: wallet_balance 属用户私有资产，可提现。

    Args:
        session:   数据库事务会话
        user_id:   用户ID (收款方)
        order_id:  订单ID
        amount:    增加金额 (Decimal, >0)
        log_type:  流水类型 (SETTLEMENT/PENALTY_INCOME/WITHDRAW_REJECT)
        idempotent_key: 幂等键
        remark:    备注

    Returns:
        PaymentLog 流水记录
    """
    _validate_amount(amount, "钱包入账")

    lock = RedisLock(_lock_key(user_id, "wallet_incr"), timeout=10)
    async with lock as acquired:
        if not acquired:
            raise AppException(
                code=ErrorCode.PAYMENT_LOCK_FAILED,
                message="资金操作繁忙，请稍后重试",
            )

        user = await _lock_user_row(session, user_id)

        before = user.wallet_balance
        after = before + amount

        await session.execute(
            text(
                "UPDATE t_users SET wallet_balance = wallet_balance + :amt "
                "WHERE id = :uid"
            ),
            {"amt": amount, "uid": user_id},
        )

        return await _insert_log_and_commit(
            session,
            user_id=user_id,
            order_id=order_id,
            log_type=log_type,
            amount=amount,
            balance_before=before,
            balance_after=after,
            target_balance_type=BalanceType.WALLET,
            idempotent_key=idempotent_key,
            remark=remark,
        )


# ══════════════════════════════════════════════════════════
# 操作 4: 钱包余额扣减
# PRD 4.10: 提现申请时 wallet_balance -= amount
# ══════════════════════════════════════════════════════════


async def wallet_balance_decrease(
    session: AsyncSession,
    *,
    user_id: int,
    amount: Decimal,
    idempotent_key: str,
    remark: str = "",
) -> PaymentLog:
    """
    钱包余额扣减 — wallet_balance -= amount。

    PRD 4.10 提现: 申请提交后扣减钱包余额，流水标记"提现中"。
    余额不足或低于最低提现门槛(10元)时拒绝。

    Args:
        session:  数据库事务会话
        user_id:  用户ID
        amount:   扣减金额 (Decimal, >0)
        idempotent_key: 幂等键
        remark:   备注

    Returns:
        PaymentLog 流水记录 (order_id=None, type=WITHDRAW_FREEZE)

    Raises:
        AppException: PAYMENT_WALLET_INSUFFICIENT / WITHDRAW_BELOW_MINIMUM
    """
    _validate_amount(amount, "提现")

    # PRD 4.10: 最低提现金额 10 元
    min_withdraw = Decimal(str(settings.WITHDRAW_MIN_AMOUNT))
    if amount < min_withdraw:
        raise AppException(
            code=ErrorCode.WITHDRAW_BELOW_MINIMUM,
            message=f"最低提现金额为 {min_withdraw} 元，当前: {amount} 元",
        )

    lock = RedisLock(_lock_key(user_id, "wallet_decr"), timeout=10)
    async with lock as acquired:
        if not acquired:
            raise AppException(
                code=ErrorCode.PAYMENT_LOCK_FAILED,
                message="资金操作繁忙，请稍后重试",
            )

        user = await _lock_user_row(session, user_id)

        if user.wallet_balance < amount:
            raise AppException(
                code=ErrorCode.PAYMENT_WALLET_INSUFFICIENT,
                message=f"可提现余额不足: 当前 {user.wallet_balance}，需要 {amount}",
            )

        before = user.wallet_balance
        after = before - amount

        await session.execute(
            text(
                "UPDATE t_users SET wallet_balance = wallet_balance - :amt "
                "WHERE id = :uid AND wallet_balance >= :amt"
            ),
            {"amt": amount, "uid": user_id},
        )

        return await _insert_log_and_commit(
            session,
            user_id=user_id,
            order_id=None,  # 提现不关联订单
            log_type=PaymentLogType.WITHDRAW_FREEZE,
            amount=-amount,  # 负值表示出账
            balance_before=before,
            balance_after=after,
            target_balance_type=BalanceType.WALLET,
            idempotent_key=idempotent_key,
            remark=remark or f"提现申请 {amount} 元",
        )


# ══════════════════════════════════════════════════════════
# 操作 5: 冻结转钱包 — 核销结算
# PRD 4.5: frozen_balance 扣订单总金额 → 扣3%服务费 → 剩余入 wallet_balance
# ══════════════════════════════════════════════════════════


async def frozen_to_wallet_settlement(
    session: AsyncSession,
    *,
    user_id: int,
    order_id: int,
    total_amount: Decimal,
    idempotent_key: str,
) -> tuple[PaymentLog, PaymentLog]:
    """
    核销结算 — 冻结余额扣订单总金额 → 扣 3% 服务费 → 剩余转入钱包。

    PRD 4.5 主流程 (核销结算):
      1. 从团长 frozen_balance 中扣除订单总金额
      2. 扣除 3% 平台服务费 (PLATFORM_SERVICE_FEE_RATE = 0.03)
      3. 剩余金额 (97%) 转入团长 wallet_balance
      4. 写入两条流水: 结算收入 + 服务费扣除

    操作在一个事务内完成。

    Args:
        session:      数据库事务会话
        user_id:      团长用户ID
        order_id:     订单ID
        total_amount: 订单总金额 (所有拼友支付的总额)
        idempotent_key: 幂等键

    Returns:
        (settlement_log, fee_log) 两条流水记录

    Raises:
        AppException: PAYMENT_FROZEN_INSUFFICIENT / PAYMENT_AMOUNT_INVALID
    """
    _validate_amount(total_amount, "核销结算")

    # 计算 3% 服务费 (Decimal 精确运算，禁止浮点数)
    fee_rate = Decimal(str(settings.PLATFORM_SERVICE_FEE_RATE))  # 0.03
    service_fee = (total_amount * fee_rate).quantize(Decimal("0.01"))
    wallet_income = total_amount - service_fee

    lock = RedisLock(_lock_key(user_id, "settlement"), timeout=15)
    async with lock as acquired:
        if not acquired:
            raise AppException(
                code=ErrorCode.PAYMENT_LOCK_FAILED,
                message="结算操作繁忙，请稍后重试",
            )

        user = await _lock_user_row(session, user_id)

        # 校验冻结余额充足
        if user.frozen_balance < total_amount:
            raise AppException(
                code=ErrorCode.PAYMENT_FROZEN_INSUFFICIENT,
                message=f"冻结余额不足无法结算: 当前 {user.frozen_balance}，需要 {total_amount}",
            )

        frozen_before = user.frozen_balance
        wallet_before = user.wallet_balance

        # Step 1: frozen_balance -= total_amount
        await session.execute(
            text(
                "UPDATE t_users SET frozen_balance = frozen_balance - :amt "
                "WHERE id = :uid AND frozen_balance >= :amt"
            ),
            {"amt": total_amount, "uid": user_id},
        )

        # Step 2: wallet_balance += wallet_income
        await session.execute(
            text(
                "UPDATE t_users SET wallet_balance = wallet_balance + :amt "
                "WHERE id = :uid"
            ),
            {"amt": wallet_income, "uid": user_id},
        )

        # 写入流水 1: 结算收入 (金额 = wallet_income)
        settlement_log_idempotent = f"{idempotent_key}:settlement"
        settlement_log = PaymentLog(
            log_no=_gen_log_no(),
            user_id=user_id,
            order_id=order_id,
            type=PaymentLogType.SETTLEMENT.value,
            amount=wallet_income,
            balance_before=wallet_before,
            balance_after=wallet_before + wallet_income,
            target_balance_type=BalanceType.WALLET.value,
            status=1,
            remark=f"订单 {order_id} 核销结算: 总金额 {total_amount} - 服务费 {service_fee} = {wallet_income}",
            idempotent_key=settlement_log_idempotent,
        )
        session.add(settlement_log)

        # 写入流水 2: 服务费扣除 (从冻结中扣除)
        fee_log_idempotent = f"{idempotent_key}:fee"
        fee_log = PaymentLog(
            log_no=_gen_log_no(),
            user_id=user_id,
            order_id=order_id,
            type=PaymentLogType.SERVICE_FEE.value,
            amount=-service_fee,  # 负值表示平台抽成
            balance_before=frozen_before,
            balance_after=frozen_before - total_amount,
            target_balance_type=BalanceType.FROZEN.value,
            status=1,
            remark=f"订单 {order_id} 平台服务费 {fee_rate*100}%: {service_fee}",
            idempotent_key=fee_log_idempotent,
        )
        session.add(fee_log)

        try:
            await session.commit()
            logger.info(
                f"核销结算完成: user={user_id} order={order_id} "
                f"total={total_amount} fee={service_fee} income={wallet_income}"
            )
            return settlement_log, fee_log
        except Exception as e:
            await session.rollback()
            if "Duplicate entry" in str(e) and "idempotent_key" in str(e):
                logger.warning(f"幂等拦截: 重复核销结算 idempotent={idempotent_key[:12]}...")
                existing = await _find_existing_by_idempotent(session, settlement_log_idempotent)
                existing_fee = await _find_existing_by_idempotent(session, fee_log_idempotent)
                if existing and existing_fee:
                    return existing, existing_fee
            raise AppException(
                code=ErrorCode.PAYMENT_SETTLEMENT_FAILED,
                message="核销结算失败",
            )


# ══════════════════════════════════════════════════════════
# 操作 6: 资金划转 — 违约金划转
# PRD 4.6: 拼友 frozen_balance → 团长 wallet_balance
# ══════════════════════════════════════════════════════════


async def transfer_penalty(
    session: AsyncSession,
    *,
    from_user_id: int,
    to_user_id: int,
    order_id: int,
    amount: Decimal,
    idempotent_key: str,
) -> tuple[PaymentLog, PaymentLog]:
    """
    资金划转 — 违约金从拼友冻结余额直接划转至团长钱包。

    PRD 4.6 阶梯退款:
      状态1 (PURCHASING) 退出时扣除 20%-30% 违约金，
      违约金直接划转至团长 wallet_balance 作为补偿。

    两步在同一事务内完成:
      1. from_user.frozen_balance -= amount (拼友冻结扣减)
      2. to_user.wallet_balance += amount   (团长钱包增加)

    Args:
        session:       数据库事务会话
        from_user_id:  出账方用户ID (拼友)
        to_user_id:    入账方用户ID (团长)
        order_id:      订单ID
        amount:        划转金额 (违约金)
        idempotent_key: 幂等键

    Returns:
        (debit_log, credit_log) 两条流水

    Raises:
        AppException: PAYMENT_FROZEN_INSUFFICIENT / PAYMENT_AMOUNT_INVALID
    """
    _validate_amount(amount, "违约金划转")

    # 同时对两个用户加锁 (按 ID 排序防死锁)
    lock_ids = sorted([from_user_id, to_user_id])
    lock = RedisLock(f"balance:transfer:{lock_ids[0]}:{lock_ids[1]}", timeout=15)
    async with lock as acquired:
        if not acquired:
            raise AppException(
                code=ErrorCode.PAYMENT_LOCK_FAILED,
                message="资金划转繁忙，请稍后重试",
            )

        # 行级锁读取双方
        from_user = await _lock_user_row(session, from_user_id)
        to_user = await _lock_user_row(session, to_user_id)

        # 校验出账方冻结余额
        if from_user.frozen_balance < amount:
            raise AppException(
                code=ErrorCode.PAYMENT_FROZEN_INSUFFICIENT,
                message=f"冻结余额不足: 拼友 {from_user_id} 当前冻结 {from_user.frozen_balance}，需要 {amount}",
            )

        from_frozen_before = from_user.frozen_balance
        to_wallet_before = to_user.wallet_balance

        # Step 1: 拼友冻结余额扣减
        await session.execute(
            text(
                "UPDATE t_users SET frozen_balance = frozen_balance - :amt "
                "WHERE id = :uid AND frozen_balance >= :amt"
            ),
            {"amt": amount, "uid": from_user_id},
        )

        # Step 2: 团长钱包余额增加
        await session.execute(
            text(
                "UPDATE t_users SET wallet_balance = wallet_balance + :amt "
                "WHERE id = :uid"
            ),
            {"amt": amount, "uid": to_user_id},
        )

        # 写入流水 1: 出账 (拼友冻结扣减)
        debit_idempotent = f"{idempotent_key}:debit"
        debit_log = PaymentLog(
            log_no=_gen_log_no(),
            user_id=from_user_id,
            order_id=order_id,
            type=PaymentLogType.PENALTY_INCOME.value,
            amount=-amount,  # 出账
            balance_before=from_frozen_before,
            balance_after=from_frozen_before - amount,
            target_balance_type=BalanceType.FROZEN.value,
            status=1,
            remark=f"订单 {order_id} 违约金扣减 → 团长 {to_user_id}",
            idempotent_key=debit_idempotent,
        )
        session.add(debit_log)

        # 写入流水 2: 入账 (团长钱包增加)
        credit_idempotent = f"{idempotent_key}:credit"
        credit_log = PaymentLog(
            log_no=_gen_log_no(),
            user_id=to_user_id,
            order_id=order_id,
            type=PaymentLogType.PENALTY_INCOME.value,
            amount=amount,  # 入账
            balance_before=to_wallet_before,
            balance_after=to_wallet_before + amount,
            target_balance_type=BalanceType.WALLET.value,
            status=1,
            remark=f"订单 {order_id} 违约金收入 ← 拼友 {from_user_id} 退款违约金",
            idempotent_key=credit_idempotent,
        )
        session.add(credit_log)

        try:
            await session.commit()
            logger.info(
                f"违约金划转完成: from={from_user_id} to={to_user_id} "
                f"order={order_id} amount={amount}"
            )
            return debit_log, credit_log
        except Exception as e:
            await session.rollback()
            if "Duplicate entry" in str(e) and "idempotent_key" in str(e):
                logger.warning(f"幂等拦截: 重复资金划转 idempotent={idempotent_key[:12]}...")
                existing_debit = await _find_existing_by_idempotent(session, debit_idempotent)
                existing_credit = await _find_existing_by_idempotent(session, credit_idempotent)
                if existing_debit and existing_credit:
                    return existing_debit, existing_credit
            raise AppException(
                code=ErrorCode.PAYMENT_SETTLEMENT_FAILED,
                message="违约金划转失败",
            )


# ══════════════════════════════════════════════════════════
# 行级锁辅助函数
# ══════════════════════════════════════════════════════════


async def _lock_user_row(session: AsyncSession, user_id: int) -> User:
    """
    使用 SELECT ... FOR UPDATE 行级锁读取用户记录。

    这是 PRD 5.1 双重防护的第二层:
      第一层: Redis 分布式锁 (业务层)
      第二层: MySQL 行级锁 (数据库层)

    Raises:
        AppException: 用户不存在
    """
    from sqlalchemy import select

    result = await session.execute(
        select(User).where(User.id == user_id).with_for_update()
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise AppException(
            code=ErrorCode.PAYMENT_USER_NOT_FOUND,
            message=f"用户不存在: {user_id}",
        )
    return user
