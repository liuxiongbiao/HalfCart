"""
对账管理 API (运营侧) — /api/v1/admin/reconciliation/*
=====================================================
PRD 4.5: 对账记录查询 / 异常明细 / 手动触发 / 待人工处理。
"""

from typing import Optional

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_required
from app.core.database import async_session_factory, get_db
from app.schemas.common import APIResponse, PaginationResponse
from app.services import reconciliation_service

router = APIRouter()


@router.get("/list", summary="对账记录列表")
async def list_records(
    recon_type: Optional[str] = Query(default=None, pattern="^(incremental|full)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _admin: int = Depends(admin_required),
):
    records, total = await reconciliation_service.list_records(db, recon_type, page, page_size)
    items = [{
        "recon_id": r.id, "recon_type": r.recon_type,
        "cycle_start": r.cycle_start.isoformat() if r.cycle_start else None,
        "cycle_end": r.cycle_end.isoformat() if r.cycle_end else None,
        "orders_checked": r.orders_checked, "users_checked": r.users_checked,
        "anomalies_total": r.anomalies_total, "auto_fixed": r.auto_fixed,
        "pending_manual": r.pending_manual, "status": r.status,
        "duration_ms": r.duration_ms,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in records]
    return APIResponse.success(data=PaginationResponse(total=total, page=page, page_size=page_size, items=items).model_dump())


@router.get("/{record_id}/details", summary="对账异常明细")
async def anomaly_details(
    record_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _admin: int = Depends(admin_required),
):
    anomalies, total = await reconciliation_service.list_anomalies(db, record_id=record_id, page=page, page_size=page_size)
    items = [{
        "anomaly_id": a.id, "record_id": a.record_id,
        "anomaly_type": a.anomaly_type, "order_id": a.order_id, "user_id": a.user_id,
        "description": a.description, "fix_status": a.fix_status, "fix_remark": a.fix_remark,
        "expected_value": a.expected_value, "actual_value": a.actual_value,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in anomalies]
    return APIResponse.success(data=PaginationResponse(total=total, page=page, page_size=page_size, items=items).model_dump())


@router.get("/pending", summary="待人工处理异常")
async def pending(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _admin: int = Depends(admin_required),
):
    anomalies, total = await reconciliation_service.list_anomalies(db, fix_status="manual_required", page=page, page_size=page_size)
    items = [{
        "anomaly_id": a.id, "anomaly_type": a.anomaly_type,
        "order_id": a.order_id, "user_id": a.user_id,
        "description": a.description, "expected_value": a.expected_value, "actual_value": a.actual_value,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in anomalies]
    return APIResponse.success(data=PaginationResponse(total=total, page=page, page_size=page_size, items=items).model_dump())


@router.post("/trigger/manual", summary="手动触发增量对账")
async def trigger_manual(
    db: AsyncSession = Depends(get_db),
    _admin: int = Depends(admin_required),
):
    record = await reconciliation_service.run_incremental_reconciliation(db)
    return APIResponse.success(data={"recon_id": record.id, "anomalies": record.anomalies_total}, message="增量对账已执行")
