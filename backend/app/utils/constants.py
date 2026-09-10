"""
全局枚举常量
===========
所有业务状态、类型的枚举定义。对应底座设计中的字段注释。
PRD 4.2: 订单4态
PRD 2.5/4.5: 资金流水类型
PRD 4.9: 纠纷类型
PRD 3.2: 信用分变化规则
"""

from enum import IntEnum


# ══════════════════════════════════════════════
# 订单状态 (PRD 4.2 订单状态机)
# 底座设计 2.3: t_orders.status
# ══════════════════════════════════════════════

class OrderStatus(IntEnum):
    """订单完整状态枚举 (单向流转 + 解散)。

    PRD 4.2 状态流转:
        GATHERING → PURCHASING → DELIVERING → FINISHED
        GATHERING → DISBANDED (超时/团长取消)
        PURCHASING → GATHERING (拼友退款后未满员 — 业务规则, 本模块仅做状态校验)
    """
    GATHERING = 0     # 拼单集资中 — 可加入、可全额退款
    PURCHASING = 1    # 成团采购中 — 资金冻结、退出扣违约金
    DELIVERING = 2    # 买到待交接 — 生成核销码、激活IM
    FINISHED = 3      # 已核销完成 — 资金清算、开启评价
    DISBANDED = 4     # 已解散 — 订单终止, 不可再操作

    @classmethod
    def label(cls, status: int) -> str:
        """获取状态中文标签。"""
        labels = {
            0: "拼单中",
            1: "采购中",
            2: "待交接",
            3: "已完成",
            4: "已解散",
        }
        return labels.get(status, "未知状态")

    @classmethod
    def is_terminal(cls, status: int) -> bool:
        """判断是否终态 (不可再流转)。"""
        return status in (cls.FINISHED, cls.DISBANDED)

    @classmethod
    def can_refund(cls, status: int) -> bool:
        """判断当前状态是否可主动退款 (PRD 4.6 阶梯退款)。"""
        return status in (cls.GATHERING, cls.PURCHASING)

    @classmethod
    def refund_ratio(cls, status: int) -> float:
        """获取退款比例 (PRD 4.6 阶梯退款机制)。"""
        ratios = {
            cls.GATHERING: 1.00,   # 全额退款
            cls.PURCHASING: 0.80,  # 扣20%违约金
            cls.DELIVERING: 0.00,  # 禁止主动退款
            cls.FINISHED: 0.00,    # 已完成不可退款
        }
        return ratios.get(status, 0.0)


# ══════════════════════════════════════════════
# 订单类型
# 底座设计 2.3: t_orders.order_type
# ══════════════════════════════════════════════

class OrderType(IntEnum):
    """订单类型。"""
    NORMAL = 1   # 普通拼单 (PRD 4.7 AI语音发单)
    SPOT = 2     # 现货转让 (PRD 4.8 OCR现货)


# ══════════════════════════════════════════════
# 资金流水类型
# 底座设计 2.5: t_payment_logs.type
# ══════════════════════════════════════════════

class PaymentLogType(IntEnum):
    """资金流水类型 (PRD 3.2/4.5 虚拟账本)。"""
    PAYMENT_FREEZE = 1     # 支付冻结 (拼友支付后资金托管)
    REFUND_UNFREEZE = 2    # 退款解冻 (拼友退出退款)
    SETTLEMENT = 3         # 核销结算 (团长frozen→wallet)
    PENALTY_INCOME = 4     # 违约金收入 (划转至团长钱包)
    SERVICE_FEE = 5        # 服务费扣除 (平台3%抽成)
    WITHDRAW_FREEZE = 6    # 提现冻结 (申请提交后扣减余额)
    WITHDRAW_PAID = 7      # 提现打款 (线下打款完成)
    WITHDRAW_REJECT = 8    # 提现退回 (打款失败退回余额)
    DISPUTE_REFUND = 9     # 纠纷退款 (AI裁判后执行)

    @classmethod
    def label(cls, log_type: int) -> str:
        """获取流水类型中文标签。"""
        labels = {
            1: "支付冻结",
            2: "退款解冻",
            3: "核销结算",
            4: "违约金收入",
            5: "平台服务费",
            6: "提现冻结",
            7: "提现打款",
            8: "提现退回",
            9: "纠纷退款",
        }
        return labels.get(log_type, "未知")


# ══════════════════════════════════════════════
# 目标余额类型
# 底座设计 2.5: t_payment_logs.target_balance_type
# ══════════════════════════════════════════════

class BalanceType(IntEnum):
    """账户类型 (PRD 3.2 虚拟账本)。"""
    WALLET = 1   # 可提现钱包余额 (wallet_balance)
    FROZEN = 2   # 冻结担保金 (frozen_balance)


# ══════════════════════════════════════════════
# 参与人支付状态
# 底座设计 2.4: t_order_participants.pay_status
# ══════════════════════════════════════════════

class ParticipantPayStatus(IntEnum):
    """参与人支付状态。"""
    PENDING = 0     # 待支付
    PAID = 1        # 已支付 (资金冻结)
    REFUNDED = 2    # 已退款
    SETTLED = 3     # 已结算 (核销完成)


# ══════════════════════════════════════════════
# 参与人角色
# 底座设计 2.4: t_order_participants.role
# ══════════════════════════════════════════════

class ParticipantRole(IntEnum):
    """订单参与人角色 (PRD 4.4 双视角)。"""
    CREATOR = 1      # 团长
    PARTICIPANT = 2  # 拼友


# ══════════════════════════════════════════════
# 用户状态
# 底座设计 2.2: t_users.status
# ══════════════════════════════════════════════

class UserStatus(IntEnum):
    """用户账号状态。"""
    ACTIVE = 1   # 正常
    BANNED = 0   # 封禁


# ══════════════════════════════════════════════
# 纠纷类型 (PRD 4.9 AI纠纷裁判)
# ══════════════════════════════════════════════

class DisputeType(IntEnum):
    """纠纷类型。"""
    DAMAGED = 1          # 商品破损
    WRONG_ITEM = 2        # 货不对板
    QUALITY_ISSUE = 3     # 质量问题
    OTHER = 9             # 其他

    @classmethod
    def label(cls, dispute_type: int) -> str:
        labels = {1: "商品破损", 2: "货不对板", 3: "质量问题", 9: "其他"}
        return labels.get(dispute_type, "未知")


# ══════════════════════════════════════════════
# 纠纷状态
# 底座设计 2.3: t_orders.dispute_status
# ══════════════════════════════════════════════

class DisputeStatus(IntEnum):
    """纠纷处理状态 (PRD 4.9)。"""
    PENDING = 0          # 处理中 (已提交, 等待AI裁判/人工)
    RESOLVED = 1         # 已完成 (AI自动判责+资金执行)
    PENDING_MANUAL = 2   # 待人工处理 (AI置信度不足)
    CLOSED = 3           # 已关闭


# ══════════════════════════════════════════════
# 提现状态
# 底座设计 2.7: t_withdraw_applications.status
# ══════════════════════════════════════════════

class WithdrawStatus(IntEnum):
    """提现申请状态 (PRD 4.10)。"""
    PENDING = 0          # 待审核 (申请提交)
    COMPLETED = 1        # 已打款 (审核通过+打款成功)
    REJECTED = 2         # 审核拒绝 (退回余额)
    DISBURSEMENT_FAILED = 3  # 打款失败 (审核通过但打款异常, 退回余额)


# ══════════════════════════════════════════════
# 收款方式
# 底座设计 2.7: t_withdraw_applications.pay_method
# ══════════════════════════════════════════════

class PayMethod(IntEnum):
    """收款方式 (PRD 4.10)。"""
    WECHAT = 1    # 微信
    ALIPAY = 2    # 支付宝


# ══════════════════════════════════════════════
# 聊天消息类型
# 底座设计 2.8: t_chat_messages.message_type
# ══════════════════════════════════════════════

class MessageType(IntEnum):
    """聊天消息类型 (PRD 3.1 履约通信)。"""
    TEXT = 1        # 文本消息
    IMAGE = 2       # 图片消息
    SYSTEM = 3      # 系统通知


# ══════════════════════════════════════════════
# 信用分变动规则 (PRD 3.2)
# ══════════════════════════════════════════════

class CreditChange(IntEnum):
    """信用分变动规则 (PRD 3.2/4.6)。

    PRD 3.2: 初始100分，跳车-1，完成核销+1
    PRD 4.6: PURCHASING阶段跳车-3分
    """
    INITIAL = 100           # 初始信用分
    JOIN_QUIT = -1          # GATHERING阶段退出 (PRD 4.6)
    PURCHASE_QUIT = -3      # PURCHASING阶段退出 (PRD 4.6)
    COMPLETE_ORDER = +1     # 完成核销 (PRD 3.2)
    DISPUTE_LOSE = -1       # 纠纷败诉 (PRD 4.9)
