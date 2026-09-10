"""
幂等键生成与校验工具
====================
PRD 5.3 资金安全: 所有资金操作支持幂等，重复请求不产生重复交易。

幂等键公式 (底座设计 2.5):
    SHA256(business_scene + ":" + order_id + ":" + user_id + ":" + operation_type)

用途:
    - 支付、退款、核销结算等资金操作前生成幂等键
    - 写入 t_payment_logs.idempotent_key 字段 (UNIQUE索引)
    - 数据库 UNIQUE 约束兜底，重复插入自动拦截
"""

import hashlib
from enum import Enum


class BusinessScene(str, Enum):
    """
    业务场景枚举 (对应底座设计 2.5 t_payment_logs.type)。

    每个场景对应一种资金流水类型。
    """
    PAYMENT_FREEZE = "payment.freeze"         # 支付冻结 (type=1)
    REFUND_UNFREEZE = "refund.unfreeze"       # 退款解冻 (type=2)
    SETTLEMENT = "settlement"                 # 核销结算 (type=3)
    PENALTY_INCOME = "penalty.income"         # 违约金收入 (type=4)
    SERVICE_FEE = "service.fee"               # 服务费扣除 (type=5)
    WITHDRAW_FREEZE = "withdraw.freeze"       # 提现冻结 (type=6)
    WITHDRAW_PAID = "withdraw.paid"           # 提现打款 (type=7)
    WITHDRAW_REJECT = "withdraw.reject"       # 提现退回 (type=8)
    DISPUTE_REFUND = "dispute.refund"         # 纠纷退款 (type=9)


def generate_idempotent_key(
    scene: BusinessScene | str,
    user_id: int,
    order_id: int | None = None,
    *extra: str,
) -> str:
    """
    生成幂等键 (SHA256)。

    PRD 5.3: 幂等键 = SHA256(业务场景 + 订单号 + 用户ID + 操作类型 + ...)
    存储于 t_payment_logs.idempotent_key CHAR(64) UNIQUE。

    Args:
        scene: 业务场景 (使用 BusinessScene 枚举)
        user_id: 用户ID
        order_id: 订单ID (可选，提现场景不关联订单)
        *extra: 额外幂等参数 (如退款单号、分期序号等)

    Returns:
        64位十六进制 SHA256 哈希字符串

    Example:
        >>> key = generate_idempotent_key(
        ...     BusinessScene.PAYMENT_FREEZE,
        ...     user_id=1001,
        ...     order_id=20260701001,
        ... )
        >>> len(key)
        64
    """
    raw = f"{scene}:{order_id or 0}:{user_id}"
    if extra:
        raw += ":" + ":".join(str(e) for e in extra)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_payment_idempotent_key(
    user_id: int,
    order_id: int,
) -> str:
    """
    支付冻结幂等键。
    同一用户在同一订单的支付操作仅执行一次。
    """
    return generate_idempotent_key(
        BusinessScene.PAYMENT_FREEZE,
        user_id=user_id,
        order_id=order_id,
    )


def generate_refund_idempotent_key(
    user_id: int,
    order_id: int,
    participant_id: int,
) -> str:
    """
    退款幂等键。
    同一参与人记录仅退款一次。
    """
    return generate_idempotent_key(
        BusinessScene.REFUND_UNFREEZE,
        user_id,
        order_id,
        f"participant:{participant_id}",
    )


def generate_settlement_idempotent_key(
    user_id: int,
    order_id: int,
) -> str:
    """
    核销结算幂等键。
    同一订单同一团长仅结算一次。
    """
    return generate_idempotent_key(
        BusinessScene.SETTLEMENT,
        user_id=user_id,
        order_id=order_id,
    )


def generate_withdraw_idempotent_key(
    user_id: int,
    withdraw_application_id: int,
) -> str:
    """
    提现幂等键。
    同一提现申请仅打款/退回一次。
    """
    return generate_idempotent_key(
        BusinessScene.WITHDRAW_PAID,
        user_id,
        None,
        f"withdraw:{withdraw_application_id}",
    )


def generate_dispute_refund_idempotent_key(
    user_id: int,
    order_id: int,
    dispute_id: int,
) -> str:
    """
    纠纷退款幂等键。
    同一纠纷仅执行一次退款。
    """
    return generate_idempotent_key(
        BusinessScene.DISPUTE_REFUND,
        user_id,
        order_id,
        f"dispute:{dispute_id}",
    )


def generate_penalty_transfer_idempotent_key(
    from_user_id: int,
    to_user_id: int,
    order_id: int,
) -> str:
    """
    违约金划转幂等键。
    同一订单同一拼友→团长的划转仅执行一次。
    """
    return generate_idempotent_key(
        BusinessScene.PENALTY_INCOME,
        from_user_id,
        order_id,
        f"to:{to_user_id}",
    )


def generate_freeze_balance_idempotent_key(
    user_id: int,
    order_id: int,
    operation: str,
) -> str:
    """
    冻结余额操作幂等键 (冻结增加/扣减)。
    operation: "incr" | "decr"
    """
    return generate_idempotent_key(
        BusinessScene.PAYMENT_FREEZE if operation == "incr" else BusinessScene.REFUND_UNFREEZE,
        user_id=user_id,
        order_id=order_id,
    )
