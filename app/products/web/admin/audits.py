"""Persistent request-audit endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.platform.request_audit import get_item, list_items, summary

router = APIRouter(prefix="/audits", tags=["Admin - Request Audits"])


@router.get("")
async def list_request_audits(
    limit: int = Query(200, ge=1, le=1_000),
    query: str = Query("", max_length=255),
    provider: str = Query("", max_length=64),
    status: str = Query("", max_length=8),
):
    return {"items": list_items(limit=limit, query=query, provider=provider, status=status)}


@router.get("/summary")
async def request_audit_summary():
    return summary()


@router.get("/{audit_id}")
async def request_audit_detail(audit_id: str):
    item = get_item(audit_id)
    if item is None:
        raise HTTPException(404, "Request audit not found")
    return item


__all__ = ["router"]
