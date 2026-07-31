"""Admin APIs for the isolated Grok Build OAuth (CPA Auth) account pool."""
from __future__ import annotations

import asyncio

from typing import Any

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from app.control.account.quota_defaults import supports_mode
from app.control.account.state_machine import is_manageable
from app.control.build import store
from app.control.build.accounts import _now_ms, refresh_account
from app.control.build.client import fetch_models
from app.control.build.routes import store as route_store
from app.control.model.registry import MODELS
from app.platform.config.snapshot import get_config
from app.platform.errors import ValidationError

router = APIRouter(prefix="/build", tags=["Admin - Grok Build"])


class ToggleRequest(BaseModel):
    enabled: bool


class DeleteRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


@router.get("/accounts")
async def list_accounts(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=2_000),
    query: str = Query("", max_length=255),
    status: str = Query("", max_length=64),
):
    rows = store.list()
    needle = query.strip().lower()
    status_filter = status.strip().lower()
    if needle:
        rows = [
            item for item in rows
            if needle in " ".join(
                str(item.get(key) or "")
                for key in ("email", "user_id", "id", "models", "last_error", "state_reason")
            ).lower()
        ]
    if status_filter:
        rows = [item for item in rows if str(item.get("status") or "").lower() == status_filter]
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    start = (current_page - 1) * page_size
    return {
        "items": rows[start:start + page_size],
        "known_models": store.known_models(),
        "total": total,
        "page": current_page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.get("/accounts/{account_id}")
async def get_account(account_id: str):
    item = next((row for row in store.list() if row["id"] == account_id), None)
    if item is None:
        raise HTTPException(404, "Build account not found")
    return {"item": item}


@router.post("/import")
async def import_cpa_auth(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith((".json", ".zip")):
        raise HTTPException(400, "Only CPA Auth JSON or ZIP files are supported")
    data = await file.read()
    if not data or len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Upload must be between 1 byte and 20 MiB")
    try:
        result, names = store.import_bytes(data)
    except (ValidationError, ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {**result, "files": names, "message": "Imported. Sync models before the account is exposed to /v1/models."}


async def _sync_models(account_id: str, *, force_oauth_refresh: bool) -> dict[str, Any]:
    account = store.get(account_id)
    if account is None:
        raise HTTPException(404, "Build account not found")
    try:
        if force_oauth_refresh:
            account = await refresh_account(account, force=True)
        models = await fetch_models(account)
        route_store.sync_discovered(models)
        store.mark_verified(account_id, models)
        return next(row for row in store.list() if row["id"] == account_id)
    except Exception as exc:
        store.record_failure(account_id, exc)
        status = int(getattr(exc, "status", 502) or 502)
        raise HTTPException(status if 400 <= status < 600 else 502, f"Build account verification failed: {exc}") from exc


@router.post("/accounts/{account_id}/sync")
async def sync_account(account_id: str):
    return {"item": await _sync_models(account_id, force_oauth_refresh=False)}


@router.post("/accounts/{account_id}/verify")
async def verify_account(account_id: str):
    """Force an OAuth refresh then fetch models before restoring the account."""
    return {"item": await _sync_models(account_id, force_oauth_refresh=True)}


class BatchVerifyRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


@router.post("/accounts/verify")
async def verify_accounts(req: BatchVerifyRequest):
    try:
        concurrency = min(max(1, int(get_config("build.verify_concurrency", 3))), 50)
    except (TypeError, ValueError):
        concurrency = 3
    semaphore = asyncio.Semaphore(concurrency)

    async def one(account_id: str) -> dict[str, Any]:
        async with semaphore:
            try:
                await _sync_models(account_id, force_oauth_refresh=True)
                return {"id": account_id, "ok": True}
            except HTTPException as exc:
                return {"id": account_id, "ok": False, "status": exc.status_code, "error": str(exc.detail)}

    rows = await asyncio.gather(*(one(account_id) for account_id in dict.fromkeys(req.ids)))
    return {"items": rows, "succeeded": sum(1 for item in rows if item["ok"]), "failed": sum(1 for item in rows if not item["ok"])}


@router.post("/accounts/{account_id}/toggle")
async def toggle_account(account_id: str, req: ToggleRequest):
    try:
        account = store.set_enabled(account_id, req.enabled)
    except ValidationError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"id": account.id, "enabled": account.enabled}


@router.delete("/accounts")
async def delete_accounts(req: DeleteRequest):
    return {"deleted": store.delete(req.ids)}


class ModelRouteRequest(BaseModel):
    public_id: str = Field(min_length=1, max_length=255)
    upstream_model: str = Field(min_length=1, max_length=255)
    enabled: bool = True


async def _ordinary_model_routes(request: Request) -> list[dict[str, Any]]:
    """Expose the configured normal-account catalog beside Build OAuth routes."""
    repo = getattr(request.app.state, "repository", None)
    pools: set[str] = set()
    if repo is not None:
        snapshot = await repo.runtime_snapshot()
        pools = {record.pool for record in snapshot.items if is_manageable(record)}

    rows: list[dict[str, Any]] = []
    for spec in MODELS:
        if spec.is_build_chat():
            continue
        available = spec.enabled and any(
            pool_name in pools and supports_mode(pool_name, int(spec.mode_id))
            for pool_name in ({0: "basic", 1: "super", 2: "heavy"}[pool_id] for pool_id in spec.pool_candidates())
        )
        rows.append(
            {
                "public_id": spec.model_name,
                "upstream_model": spec.model_name,
                "enabled": spec.enabled,
                "origin": "catalog",
                "route_kind": "ordinary",
                "available": available,
                "account_pool": spec.pool_name(),
                "display_name": spec.public_name,
            }
        )
    return rows


@router.get("/model-routes")
async def list_model_routes(
    request: Request,
    query: str = Query("", max_length=255),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=2_000),
):
    ordinary = await _ordinary_model_routes(request)
    build = [{**item, "route_kind": "build"} for item in route_store.list()]
    rows = [*ordinary, *build]
    needle = query.strip().lower()
    if needle:
        rows = [
            item for item in rows
            if needle in " ".join(
                str(item.get(key) or "")
                for key in ("public_id", "upstream_model", "display_name", "origin", "route_kind", "account_pool")
            ).lower()
        ]
    rows.sort(key=lambda item: (item.get("route_kind") != "ordinary", str(item.get("public_id") or "").lower()))
    total = len(rows)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    start = (current_page - 1) * page_size
    return {
        "items": rows[start:start + page_size],
        "total": total,
        "page": current_page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


@router.post("/model-routes")
async def save_model_route(req: ModelRouteRequest):
    try:
        route = route_store.upsert(public_id=req.public_id, upstream_model=req.upstream_model, enabled=req.enabled)
    except ValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return route.model_dump()


@router.delete("/model-routes/{public_id}")
async def delete_model_route(public_id: str):
    return {"deleted": route_store.delete(public_id)}


__all__ = ["router"]
