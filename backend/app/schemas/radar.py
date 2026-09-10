"""
LBS 雷达大厅 Schema — 请求参数 / 响应卡片
===========================================
PRD 4.3: 附近拼单瀑布流, 1.5km范围, 按距离排序, 品类筛选, 关键词搜索。
底座设计 3.2: order_index 索引字段对齐。
"""

from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.common import PaginationParams


class RadarListRequest(PaginationParams):
    """
    雷达列表查询请求参数 (PRD 4.3)。

    必填: 经纬度 (用户当前位置)
    可选: 品类筛选、关键词搜索、仅看现货、自定义距离
    """

    lat: float = Field(..., ge=-90, le=90, description="用户当前纬度")
    lng: float = Field(..., ge=-180, le=180, description="用户当前经度")
    category: Optional[str] = Field(default=None, max_length=30, description="商品品类筛选")
    keyword: Optional[str] = Field(default=None, max_length=50, description="商品名称/描述全文搜索")
    spot_only: bool = Field(default=False, description="仅看现货订单 (PRD 4.8)")
    radius_km: float = Field(
        default=1.5, ge=0.1, le=5.0,
        description="搜索半径(km), 默认1.5, 最大5 (PRD 4.3)"
    )


class RadarOrderCard(BaseModel):
    """
    雷达列表卡片响应 — 前端瀑布流渲染 (PRD 4.3 界面要求)。

    展示字段: 商品名/品类/人均价格/总人数/距离/地点/现货标签/截单时间/信用分
    """

    order_id: int = Field(..., description="订单ID")
    order_no: str = Field(..., description="业务单号")
    goods_name: str = Field(..., description="商品名称")
    goods_category: str = Field(default="", description="商品品类")
    price_per_person: str = Field(..., description="人均价格")
    total_people: int = Field(..., description="总人数 (含团长)")
    remaining_people: int = Field(..., description="剩余可加入人数")
    distance_m: float = Field(..., description="距离 (米)")
    meet_location: str = Field(..., description="交接地点 (脱敏)")
    is_spot: bool = Field(default=False, description="是否现货")
    deadline: Optional[str] = Field(default=None, description="截单时间 (ISO8601)")
    credit_score: int = Field(default=100, description="团长信用分")
    status: int = Field(..., description="订单状态")
    status_label: str = Field(default="", description="状态中文标签")
