"""
统一响应格式与通用Schema
=========================
全平台 API 统一使用此格式返回数据，确保前端解析一致性。

响应格式:
{
    "code": 0,            # 0=成功, 非0=业务错误码
    "message": "success", # 提示信息
    "data": { ... },      # 业务数据
    "trace_id": "..."     # 全链路追踪ID
}
"""

import uuid
from enum import IntEnum
from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════
# 通用错误码 (PRD 4.x 各模块异常处理)
# ══════════════════════════════════════════════

class ErrorCode(IntEnum):
    """平台统一错误码枚举。"""

    # ── 通用错误 (0-99) ──
    SUCCESS = 0
    UNKNOWN_ERROR = 1
    PARAM_VALIDATION_ERROR = 2
    RESOURCE_NOT_FOUND = 3
    PERMISSION_DENIED = 4
    RATE_LIMITED = 5
    INTERNAL_ERROR = 99

    # ── 鉴权相关 (100-199) PRD 4.1 ──
    AUTH_INVALID_CODE = 100
    AUTH_CODE_EXPIRED = 101
    AUTH_CODE_RATE_LIMITED = 102
    AUTH_TOKEN_EXPIRED = 103
    AUTH_TOKEN_INVALID = 104
    AUTH_ACCOUNT_BANNED = 105
    AUTH_PHONE_INVALID = 106
    AUTH_REGISTER_DUPLICATE = 107        # 用户名/手机号已注册
    AUTH_WEAK_PASSWORD = 108             # 密码强度不足
    AUTH_CREDENTIALS_INVALID = 109       # 账号或密码错误
    AUTH_WECHAT_BIND_FAILED = 110        # 微信绑定失败
    AUTH_WECHAT_ALREADY_BOUND = 111      # 微信已绑定其他账号
    AUTH_WECHAT_STATE_EXPIRED = 112      # 授权会话过期
    AUTH_WECHAT_TOKEN_EXPIRED = 113      # 绑定凭证过期
    AUTH_WECHAT_ACCOUNT_BANNED = 114     # 微信登录的账号被封禁
    AUTH_WECHAT_PHONE_UNVERIFIED = 115   # 手机号未验证就尝试注册

    # ── 订单相关 (200-299) PRD 4.2/4.4 ──
    ORDER_NOT_FOUND = 200
    ORDER_STATUS_INVALID = 201
    ORDER_STATUS_TRANSITION_DENIED = 202
    ORDER_FULL = 203
    ORDER_SELF_JOIN_DENIED = 204
    ORDER_DEADLINE_EXPIRED = 205
    ORDER_NOT_MEMBER = 206
    ORDER_ALREADY_JOINED = 207
    ORDER_DISBAND_DENIED = 208             # 非 GATHERING 状态不可解散
    ORDER_CREATOR_ONLY = 209               # 仅团长可操作
    ORDER_SPOT_START_STATUS = 210          # 现货订单初始状态不为2

    # ── 支付与资金 (300-399) PRD 4.5/4.6 ──
    PAYMENT_INSUFFICIENT_BALANCE = 300
    PAYMENT_DUPLICATE = 301
    PAYMENT_AMOUNT_MISMATCH = 302
    PAYMENT_SETTLEMENT_FAILED = 303
    PAYMENT_IDEMPOTENT_BLOCKED = 304       # 幂等键已存在，重复操作被拦截
    PAYMENT_FROZEN_INSUFFICIENT = 305      # 冻结余额不足
    PAYMENT_WALLET_INSUFFICIENT = 306       # 钱包余额不足
    PAYMENT_AMOUNT_INVALID = 307           # 金额无效 (≤0 或非法)
    PAYMENT_USER_NOT_FOUND = 308           # 用户不存在
    PAYMENT_LOCK_FAILED = 309              # 分布式锁获取失败
    REFUND_NOT_ALLOWED = 310
    REFUND_DUPLICATE = 311
    REFUND_BALANCE_INSUFFICIENT = 312

    # ── 核销相关 (400-429) PRD 4.5 ──
    VERIFY_CODE_ERROR = 400
    VERIFY_CODE_LOCKED = 401
    VERIFY_CODE_USED = 402
    VERIFY_ORDER_STATUS_INVALID = 403

    # ── 提现相关 (430-459) PRD 4.10 ──
    WITHDRAW_BELOW_MINIMUM = 430
    WITHDRAW_EXCEED_BALANCE = 431
    WITHDRAW_DUPLICATE_REQUEST = 432
    WITHDRAW_EXCEED_LIMIT = 433           # 超过单笔最高限额
    WITHDRAW_DAILY_LIMIT = 434            # 超过单日次数
    WITHDRAW_NOT_FOUND = 435              # 提现记录不存在
    WITHDRAW_STATUS_INVALID = 436          # 提现状态不允许当前操作
    WITHDRAW_AUDIT_DUPLICATE = 437         # 重复审核

    # ── 纠纷相关 (500-529) PRD 4.9 ──
    DISPUTE_NOT_ALLOWED = 500
    DISPUTE_DUPLICATE = 501
    DISPUTE_EXCEED_AI_LIMIT = 502
    DISPUTE_NOT_FOUND = 503              # 纠纷不存在
    DISPUTE_PERMISSION_DENIED = 504       # 无权查看该纠纷

    # ── AI 相关 (600-699) PRD 4.7/4.8 ──
    AI_ASR_FAILED = 600
    AI_NLP_PARSE_FAILED = 601
    AI_OCR_FAILED = 602
    AI_DISPUTE_JUDGE_FAILED = 603
    AI_LOW_CONFIDENCE = 604              # AI输出置信度不足
    AI_INVALID_RESPONSE = 605            # AI返回格式不可解析
    AI_WORKFLOW_NOT_FOUND = 606          # Dify工作流ID无效
    AI_AUTHENTICATION_FAILED = 607       # Dify API Key无效
    AI_RATE_LIMITED = 608                # Dify API限流
    AI_CONTENT_VIOLATION = 610
    AI_TIMEOUT = 620

    # ── LBS 相关 (700-719) PRD 4.3 ──
    LBS_LOCATION_DENIED = 700
    LBS_LOCATION_ANOMALY = 701
    LBS_NO_RESULTS = 702


# ══════════════════════════════════════════════
# 统一响应模型
# ══════════════════════════════════════════════

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    """
    统一 API 响应封装。

    所有接口通过此模型返回，确保前端解析一致性。

    Usage:
        return APIResponse.success(data=order_list)
        return APIResponse.error(code=ErrorCode.ORDER_NOT_FOUND, message="订单不存在")
    """
    code: int = Field(default=ErrorCode.SUCCESS, description="业务状态码 (0=成功)")
    message: str = Field(default="success", description="提示信息")
    data: Optional[T] = Field(default=None, description="业务数据")
    trace_id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="全链路追踪ID (PRD 5.3)",
    )

    @classmethod
    def success(cls, data: Any = None, message: str = "success") -> "APIResponse":
        """构建成功响应。"""
        return cls(code=ErrorCode.SUCCESS, message=message, data=data)

    @classmethod
    def error(
        cls,
        code: ErrorCode | int,
        message: str = "error",
        data: Any = None,
    ) -> "APIResponse":
        """构建错误响应。"""
        return cls(code=int(code), message=message, data=data)


# ══════════════════════════════════════════════
# 分页
# ══════════════════════════════════════════════

class PaginationParams(BaseModel):
    """分页请求参数基类。"""
    page: int = Field(default=1, ge=1, description="页码 (从1开始)")
    page_size: int = Field(default=20, ge=1, le=100, description="每页数量 (最大100)")


class PaginationResponse(BaseModel, Generic[T]):
    """分页响应基类。"""
    total: int = Field(default=0, description="总记录数")
    page: int = Field(default=1, description="当前页码")
    page_size: int = Field(default=20, description="每页数量")
    items: list[T] = Field(default_factory=list, description="数据列表")
