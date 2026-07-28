"""Persistent request-audit endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.platform.request_audit import get_item, list_page, summary

router = APIRouter(prefix="/audits", tags=["Admin - Request Audits"])


@router.get("")
async def list_request_audits(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=2_000),
    # Compatibility with the pre-pagination endpoint; explicit page_size wins.
    limit: int | None = Query(None, ge=10, le=2_000),
    query: str = Query("", max_length=255),
    provider: str = Query("", max_length=64),
    status: str = Query("", max_length=8),
):
    effective_size = page_size if limit is None or page_size != 50 else limit
    return list_page(page=page, page_size=effective_size, query=query, provider=provider, status=status)


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
