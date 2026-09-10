"""
对账 Schema — PRD 4.5 分布式事务补偿。
"""
from typing import Optional
from pydantic import BaseModel, Field


class ReconRecordItem(BaseModel):
    recon_id: int; recon_type: str = ""
    cycle_start: Optional[str]; cycle_end: Optional[str]
    orders_checked: int = 0; users_checked: int = 0
    anomalies_total: int = 0; auto_fixed: int = 0; pending_manual: int = 0
    status: str = ""; duration_ms: int = 0
    created_at: Optional[str] = None


class ReconAnomalyItem(BaseModel):
    anomaly_id: int; record_id: int
    anomaly_type: str = ""; order_id: Optional[int]; user_id: Optional[int]
    description: str = ""; fix_status: str = ""; fix_remark: str = ""
    expected_value: str = ""; actual_value: str = ""
    created_at: Optional[str] = None
