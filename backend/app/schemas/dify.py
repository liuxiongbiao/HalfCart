"""
Dify 工作流统一响应 Schema
============================
所有 Dify 工作流返回统一结构: { auto_fill, message, data }。
新增工作流时在此文件追加对应的 data Schema。
"""

from typing import Any, Generic, Optional, TypeVar

from pydantic import BaseModel, Field

# ── 通用泛型响应 ──

T = TypeVar("T")


class DifyWorkflowResponse(BaseModel, Generic[T]):
    """
    Dify 工作流统一响应。

    auto_fill: 置信度达标 → true (前端可自动填充表单)
              置信度不足/异常 → false (前端展示人工确认页)
    message:   提示文案 (成功/失败/降级)
    data:      工作流专属结构化数据
    """

    auto_fill: bool = Field(default=False, description="是否可自动填充表单")
    message: str = Field(default="", description="提示文案")
    data: Optional[T] = Field(default=None, description="结构化解析结果")

    @classmethod
    def success(cls, data: T, message: str = "解析成功") -> "DifyWorkflowResponse[T]":
        return cls(auto_fill=True, message=message, data=data)

    @classmethod
    def confirm(cls, data: T, message: str = "请确认解析结果") -> "DifyWorkflowResponse[T]":
        """置信度不足, 需用户确认。"""
        return cls(auto_fill=False, message=message, data=data)

    @classmethod
    def fallback(cls, message: str = "解析服务暂不可用，请手动填写") -> "DifyWorkflowResponse":
        """服务降级, 返回空数据。"""
        return cls(auto_fill=False, message=message, data=None)


# ── 拼单文本解析工作流 — 专属 Data ──


class TextParserData(BaseModel):
    """拼单文本解析工作流结构化输出 (PRD 4.7)。"""

    goods_name: str = Field(default="", description="商品名称")
    total_people: int = Field(default=2, ge=2, description="总人数 (≥2)")
    price_per_person: float = Field(default=0.0, ge=0.0, description="人均价格 (元)")
    deadline: str = Field(default="", description="截单时间 YYYY-MM-DD HH:mm")
    meet_location: str = Field(default="", description="交接地点")
    purchase_channel: str = Field(default="", description="采购渠道")
    goods_desc: str = Field(default="", description="商品备注")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0, description="置信度 0~1")


# ══════════════════════════════════════════════
# 后续新增工作流参考模板 — 在此追加 Data Schema
# ══════════════════════════════════════════════
#
# class NewWorkflowData(BaseModel):
#     """新工作流专属数据结构"""
#     field_1: str = Field(...)
#     field_2: int = Field(...)
