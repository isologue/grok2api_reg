"""Admin endpoints for the integrated browser registration worker and archive."""
from __future__ import annotations

import os
from typing import Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, RootModel

from app.control.account.commands import AccountUpsert, ListAccountsQuery
from app.control.account.repository import AccountRepository
from app.control.registration.archive import ARCHIVE_EXT_KEY, build_export_record, encrypt_profile
from app.platform.auth.middleware import get_admin_key
from . import get_repo

router = APIRouter(prefix="/registration", tags=["Admin - Registration"])


class RegistrationSettingsRequest(RootModel[dict[str, Any]]):
    pass


class RegistrationArchiveItem(BaseModel):
    token: str = Field(min_length=1)
    email: str = ""
    password: str = ""
    oauth: dict[str, Any] = Field(default_factory=dict)
    provider: str = "grok_build"


def _manager(request: Request):
    manager = getattr(request.app.state, "registration_manager", None)
    if manager is None:
        raise HTTPException(503, "Registration runtime is not initialized")
    return manager


def _error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/config")
async def get_registration_config(request: Request):
    return _manager(request).get_settings()


@router.put("/config")
async def save_registration_config(req: RegistrationSettingsRequest, request: Request):
    try:
        return _manager(request).save_settings(req.root)
    except ValueError as exc:
        raise _error(exc) from exc


@router.get("/status")
async def registration_status(request: Request):
    return _manager(request).status()


@router.post("/start")
async def start_registration(request: Request):
    try:
        port = int(os.getenv("SERVER_PORT", "8000"))
        return await _manager(request).start(admin_key=get_admin_key(), server_port=port)
    except (RuntimeError, ValueError) as exc:
        raise _error(exc) from exc


@router.post("/stop")
async def stop_registration(request: Request):
    try:
        return await _manager(request).stop()
    except RuntimeError as exc:
        raise _error(exc) from exc


@router.post("/archive/import")
async def import_registration_archives(
    items: list[RegistrationArchiveItem],
    request: Request,
    repo: AccountRepository = Depends(get_repo),
):
    """Store successful browser registrations with a Fernet-encrypted profile."""
    settings = _manager(request)._read_settings_raw()
    account = settings.get("account") or {}
    pool = str(account.get("pool") or "basic")
    tags = list(account.get("tags") or [])
    upserts: list[AccountUpsert] = []
    for item in items:
        token = item.token.strip()
        if not token:
            continue
        encrypted = encrypt_profile({
            "provider": item.provider or "grok_build",
            "email": item.email.strip(),
            "password": item.password,
            "oauth": item.oauth,
        })
        upserts.append(AccountUpsert(token=token, pool=pool, tags=tags, ext={ARCHIVE_EXT_KEY: encrypted}))
    if not upserts:
        raise HTTPException(400, "No valid registration archives provided")
    result = await repo.upsert_accounts(upserts)
    return {"count": result.upserted or len(upserts), "skipped": 0}


@router.get("/archive/export")
async def export_registration_archives(
    include_password: bool = False,
    repo: AccountRepository = Depends(get_repo),
):
    """Export decrypted profiles in the Grok Build JSON layout requested by the user."""
    records: list[Any] = []
    page = 1
    while True:
        result = await repo.list_accounts(ListAccountsQuery(page=page, page_size=2000))
        records.extend(result.items)
        if page >= result.total_pages or not result.items:
            break
        page += 1
    accounts = [
        item for record in records
        if not record.is_deleted()
        for item in [build_export_record(record.token, record.ext, include_password=include_password)]
        if item is not None
    ]
    headers = {"Content-Disposition": 'attachment; filename="grok2api-registration-archives.json"'}
    return Response(content=orjson.dumps({"accounts": accounts}), media_type="application/json", headers=headers)


__all__ = ["router"]
