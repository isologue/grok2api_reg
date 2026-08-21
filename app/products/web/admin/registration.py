"""Admin endpoints for the integrated browser registration worker."""
from __future__ import annotations

import asyncio
import contextlib
import io
import os
import re
import zipfile
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, RootModel

from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.repository import AccountRepository
from app.control.registration.archive import ARCHIVE_EXT_KEY, decrypt_profile, encrypt_profile
from app.control.registration.cpa import (
    cpa_auth_task_files,
    export_cpa_auth,
    finalize_cpa_task,
    initialize_cpa_task,
    list_cpa_auth_tasks,
    record_cpa_task_result,
)
from app.control.registration.console import translate_cpa_message
from app.platform.auth.middleware import get_admin_key
from app.platform.paths import log_path
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


class CpaRetryRequest(BaseModel):
    token: str = Field(min_length=1, max_length=8_192)
    # Optional only for external SSO imports.  Protocol mint can also recover
    # it from OAuth claims after authorization.
    email: str = Field(default="", max_length=512)


class CpaBatchRetryRequest(BaseModel):
    """Explicitly selected ordinary accounts for a serial CPA retry batch."""

    tokens: list[str] = Field(min_length=1, max_length=100)


class RegistrationCredentialsRequest(BaseModel):
    token: str = Field(min_length=1, max_length=8_192)


def _cpa_retry_runtime(request: Request) -> tuple[dict[str, dict[str, Any]], set[str], asyncio.Lock]:
    """Keep manual CPA retries isolated from the browser-registration process."""
    app_state = request.app.state
    tasks = getattr(app_state, "manual_cpa_retry_tasks", None)
    if tasks is None:
        tasks = {}
        app_state.manual_cpa_retry_tasks = tasks
    active_tokens = getattr(app_state, "manual_cpa_retry_active_tokens", None)
    if active_tokens is None:
        active_tokens = set()
        app_state.manual_cpa_retry_active_tokens = active_tokens
    lock = getattr(app_state, "manual_cpa_retry_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app_state.manual_cpa_retry_lock = lock
    return tasks, active_tokens, lock


def _manual_cpa_task_id() -> str:
    return "manual_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]


def _manual_cpa_batch_task_id() -> str:
    return "batch_" + datetime.now(UTC).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]


def _cpa_batch_retry_runtime(request: Request) -> tuple[dict[str, dict[str, Any]], str | None]:
    """Keep at most one serial bulk CPA retry queue per web process.

    Exporting a saved archive may launch a browser and rotate SSO.  A batch is
    therefore deliberately serial; it reuses the existing per-account retry
    path instead of running multiple browser/OAuth flows concurrently.
    """
    app_state = request.app.state
    tasks = getattr(app_state, "manual_cpa_batch_retry_tasks", None)
    if tasks is None:
        tasks = {}
        app_state.manual_cpa_batch_retry_tasks = tasks
    active_id = getattr(app_state, "manual_cpa_batch_retry_active_id", None)
    return tasks, active_id


def _set_active_cpa_batch_retry(request: Request, task_id: str | None) -> None:
    request.app.state.manual_cpa_batch_retry_active_id = task_id


def _classify_cpa_export_status(result: dict[str, Any]) -> str:
    """Classify only the retry outcome; normal-account availability is unchanged."""
    if bool(result.get("ok")) and result.get("path"):
        return "success"
    error = str(result.get("error") or result.get("reason") or "").casefold()
    if "invalid_grant" in error and "access denied" in error:
        return "xai_denied"
    if "device auth failed: access_denied" in error:
        return "xai_denied"
    return "retryable_failure"


async def _persist_cpa_export_status(
    repo: AccountRepository,
    *,
    token: str,
    task_id: str,
    result: dict[str, Any],
) -> None:
    """Persist a compact, non-secret CPA outcome on the ordinary account row."""
    status = _classify_cpa_export_status(result)
    message = str(result.get("error") or result.get("reason") or "").strip()[:500]
    payload = {
        "status": status,
        "updated_at": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "message": message,
    }
    await repo.patch_accounts([AccountPatch(token=token, ext_merge={"cpa_export": payload})])


def _retry_state_payload(task: dict[str, Any]) -> dict[str, Any]:
    """Return task progress without exposing archive, SSO, or OAuth material."""
    return {
        key: task.get(key)
        for key in (
            "task_id", "state", "created_at", "started_at", "finished_at", "email",
            "message", "build_imported", "models", "sso_rotated",
        )
    }


def _cpa_task_log_path(task_id: str):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task_id):
        raise ValueError("invalid CPA task id")
    directory = log_path("registration")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{task_id}.log"


def _append_cpa_task_log(task_id: str, message: str) -> None:
    """Append one UTF-8, Chinese-formatted line without retaining credentials."""
    try:
        path = _cpa_task_log_path(task_id)
        text = translate_cpa_message(message)
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} | [CPA] {text}\n")
    except OSError:
        # A failed observability write must never abort a valid OAuth export.
        return


def _read_cpa_task_log(task_id: str, *, limit: int = 300) -> list[str]:
    try:
        path = _cpa_task_log_path(task_id)
    except ValueError:
        return []
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()[-max(1, min(int(limit), 1_000)):]
    except OSError:
        return []


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


class OutlookPoolResetRequest(BaseModel):
    scope: str = Field(default="all", pattern="^(retryable|failed|busy|invalid|used|unused|delete_invalid|all)$")


@router.get("/outlook-pool/details")
async def outlook_pool_details(provider_id: str, request: Request, status: str = "all"):
    try:
        return _manager(request).outlook_pool_details(provider_id, status)
    except ValueError as exc:
        raise _error(exc) from exc


@router.post("/outlook-pool/reset")
async def reset_outlook_pool(req: OutlookPoolResetRequest, request: Request):
    """Clear only local Microsoft mailbox-pool state; mailbox credentials remain local and masked."""
    try:
        return _manager(request).reset_outlook_pool(req.scope)
    except ValueError as exc:
        raise _error(exc) from exc


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


@router.post("/archives/credentials")
async def reveal_registration_credentials(
    req: RegistrationCredentialsRequest,
    repo: AccountRepository = Depends(get_repo),
):
    """Return only the saved login pair for an authenticated administrator request."""
    token = req.token.strip()
    record = next(iter(await repo.get_accounts([token])), None)
    if record is None:
        raise HTTPException(404, "\u8d26\u53f7\u4e0d\u5b58\u5728\u6216\u5df2\u5220\u9664")
    profile = decrypt_profile(record.ext)
    if profile is None:
        raise HTTPException(409, "\u8be5\u8d26\u53f7\u6ca1\u6709\u53ef\u7528\u7684\u6ce8\u518c\u6863\u6848")
    email = str(profile.get("email") or "").strip()
    password = str(profile.get("password") or "")
    if not email or not password:
        raise HTTPException(409, "\u6ce8\u518c\u6863\u6848\u7f3a\u5c11\u90ae\u7bb1\u6216\u5bc6\u7801")
    # Deliberately omit SSO / OAuth values from this admin response.
    return {"email": email, "password": password}


@router.post("/cpa-auths/retry")
async def retry_cpa_auth_for_account(
    req: CpaRetryRequest,
    request: Request,
    repo: AccountRepository = Depends(get_repo),
):
    """Retry CPA Auth for either a registered archive or a pure external SSO.

    Registered accounts use SSO protocol first and may fall back to a browser
    login.  If that login obtains a new SSO, the normal account-pool record is
    atomically rotated while retaining its pool, tags, quotas and counters.
    External SSO imports have no password fallback, but remain eligible while
    their SSO is valid.
    """
    token = req.token.strip()
    record = next(iter(await repo.get_accounts([token])), None)
    if record is None or record.is_deleted():
        raise HTTPException(404, "\u8d26\u53f7\u4e0d\u5b58\u5728\u6216\u5df2\u5220\u9664")

    profile = decrypt_profile(record.ext)
    archive_email = str((profile or {}).get("email") or "").strip()
    password = str((profile or {}).get("password") or "")
    email = archive_email or req.email.strip()
    has_browser_fallback = bool(email and password)

    tasks, active_tokens, retry_lock = _cpa_retry_runtime(request)
    if token in active_tokens:
        raise HTTPException(409, "\u8be5\u8d26\u53f7\u7684 CPA \u5bfc\u51fa\u91cd\u8bd5\u6b63\u5728\u8fdb\u884c")

    settings = _manager(request)._read_settings_raw()
    cpa = dict(settings.get("cpa") or {})
    # This is an explicit admin action, so it is allowed even when automatic
    # post-registration export is currently disabled.
    cpa["enabled"] = True
    if not str(cpa.get("proxy") or "").strip():
        cpa["proxy"] = str(settings.get("browser_proxy") or settings.get("proxy") or "").strip()
    task_id = _manual_cpa_task_id()
    cpa["task_id"] = task_id
    initialize_cpa_task(cpa, task_id, started_at=datetime.now(UTC).isoformat(), requested_count=1)
    _append_cpa_task_log(
        task_id,
        "manual CPA retry queued (browser fallback available)" if has_browser_fallback
        else "manual CPA retry queued (external SSO protocol only)",
    )

    task: dict[str, Any] = {
        "task_id": task_id,
        "state": "queued",
        "created_at": datetime.now(UTC).isoformat(),
        "started_at": None,
        "finished_at": None,
        "email": email,
        "message": "\u5df2\u52a0\u5165 CPA \u5bfc\u51fa\u91cd\u8bd5\u961f\u5217",
        "build_imported": False,
        "sso_rotated": False,
        "models": [],
    }
    tasks[task_id] = task
    active_tokens.add(token)

    async def _run() -> None:
        status_token = token
        task["state"] = "running"
        task["started_at"] = datetime.now(UTC).isoformat()
        task["message"] = "\u6b63\u5728\u5bfc\u51fa CPA Auth"
        _append_cpa_task_log(task_id, "manual CPA retry started")
        try:
            async with retry_lock:
                result = await asyncio.to_thread(
                    export_cpa_auth,
                    account={"email": email, "password": password, "sso": record.token},
                    cpa=cpa,
                    cookies=None,
                    log=lambda message: _append_cpa_task_log(task_id, message),
                )

            # Browser fallback can create a new SSO.  It must replace the old
            # primary-token record before reporting task success, otherwise the
            # recovered normal account would remain unusable after CPA export.
            refreshed_sso = str(result.pop("refreshed_sso", "") or "").strip()
            if refreshed_sso and refreshed_sso != token:
                try:
                    rotated = await repo.rotate_account_token(token, refreshed_sso)
                    if rotated.upserted:
                        status_token = refreshed_sso
                        task["sso_rotated"] = True
                        _append_cpa_task_log(task_id, "browser login obtained a new SSO; normal account pool record rotated")
                    else:
                        _append_cpa_task_log(task_id, "browser login obtained a new SSO but the original account was changed concurrently; retained CPA export only")
                except Exception as exc:  # noqa: BLE001
                    # CPA Auth is already valid.  Keep it usable while making the
                    # persistence warning visible instead of falsely failing it.
                    _append_cpa_task_log(task_id, f"CPA Auth exported, but normal account SSO rotation failed: {type(exc).__name__}: {exc}")

            record_cpa_task_result(cpa, result)
            try:
                await _persist_cpa_export_status(
                    repo, token=status_token, task_id=task_id, result=result
                )
            except Exception as exc:  # noqa: BLE001
                _append_cpa_task_log(
                    task_id,
                    f"CPA export status persistence failed: {type(exc).__name__}: {exc}",
                )
            if not result.get("ok"):
                task["state"] = "failed"
                task["message"] = str(result.get("error") or result.get("reason") or "CPA \u5bfc\u51fa\u5931\u8d25")[:500]
                _append_cpa_task_log(task_id, f"manual CPA retry failed: {task['message']}")
                return

            resolved_email = str(result.get("email") or "").strip()
            if resolved_email:
                task["email"] = resolved_email
            probe = result.get("probe_models") if isinstance(result.get("probe_models"), dict) else {}
            model_ids = [
                str(item)
                for item in (probe.get("model_ids") or result.get("model_ids") or [])
                if str(item)
            ]
            task["models"] = model_ids
            # CPA export success must not depend on the optional /models probe.
            # Import the OAuth account even when the probe is empty, then let a
            # later model sync fill in the model list.
            if bool(cpa.get("auto_import_build", True)) and result.get("path"):
                try:
                    from app.control.build import import_cpa_auth_file
                    from app.control.build.routes import store as build_routes

                    import_cpa_auth_file(str(result["path"]), model_ids=model_ids)
                    if model_ids:
                        build_routes.sync_discovered(model_ids)
                    task["build_imported"] = True
                except Exception as exc:  # noqa: BLE001
                    task["message"] = f"CPA Auth \u5df2\u5bfc\u51fa\uff0c\u4f46\u81ea\u52a8\u5bfc\u5165 Build \u8d26\u53f7\u6c60\u5931\u8d25\uff1a{type(exc).__name__}: {exc}"[:500]
                    task["state"] = "completed"
                    _append_cpa_task_log(task_id, task["message"])
                    return
            task["state"] = "completed"
            message = "CPA Auth \u5df2\u5bfc\u51fa\u5e76\u5bfc\u5165 Build \u8d26\u53f7\u6c60" if task["build_imported"] else "CPA Auth \u5df2\u5bfc\u51fa"
            if task["build_imported"] and not model_ids:
                message += "(\u6a21\u578b\u63a2\u6d4b\u4e3a\u7a7a\uff0c\u7b49\u5f85\u540e\u7eed\u540c\u6b65)"
            if task["sso_rotated"]:
                message += "\uff0c\u666e\u901a\u8d26\u53f7\u6c60 SSO \u5df2\u66f4\u65b0"
            task["message"] = message
            _append_cpa_task_log(task_id, task["message"])
        except Exception as exc:  # noqa: BLE001
            failure_result = {"ok": False, "error": f"retry exception: {type(exc).__name__}: {exc}"}
            record_cpa_task_result(cpa, failure_result)
            try:
                await _persist_cpa_export_status(
                    repo, token=status_token, task_id=task_id, result=failure_result
                )
            except Exception as persist_exc:  # noqa: BLE001
                _append_cpa_task_log(
                    task_id,
                    f"CPA export status persistence failed: {type(persist_exc).__name__}: {persist_exc}",
                )
            task["state"] = "failed"
            task["message"] = f"CPA \u5bfc\u51fa\u5f02\u5e38\uff1a{type(exc).__name__}: {exc}"[:500]
            _append_cpa_task_log(task_id, task["message"])
        finally:
            task["finished_at"] = datetime.now(UTC).isoformat()
            finalize_cpa_task(cpa, task_id, state=task["state"], finished_at=task["finished_at"])
            active_tokens.discard(token)

    asyncio.create_task(_run(), name=f"manual-cpa-retry-{task_id}")
    return _retry_state_payload(task)


def _batch_retry_state_payload(task: dict[str, Any]) -> dict[str, Any]:
    """Return aggregate progress without exposing account or OAuth secrets."""
    return {
        key: task.get(key)
        for key in (
            "task_id", "state", "created_at", "started_at", "finished_at",
            "total", "current_index", "succeeded", "failed", "skipped",
            "cancel_requested", "message", "child_task_ids",
        )
    }


@router.post("/cpa-auths/retry/batch")
async def retry_cpa_auth_batch(
    req: CpaBatchRetryRequest,
    request: Request,
    repo: AccountRepository = Depends(get_repo),
):
    """Queue selected accounts for serial CPA Auth retries.

    The batch intentionally delegates each item to the established single
    account retry.  This preserves SSO rotation, encrypted-profile browser
    fallback, per-account CPA status persistence, task logs, and Build import.
    A cancel request stops *new* items; an already running OAuth flow is allowed
    to finish so its browser and token poller can clean up safely.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in req.tokens:
        token = str(raw or "").strip()
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    if not tokens:
        raise HTTPException(400, "\u8bf7\u81f3\u5c11\u9009\u62e9\u4e00\u4e2a\u8d26\u53f7")

    tasks, active_id = _cpa_batch_retry_runtime(request)
    if active_id:
        active = tasks.get(active_id)
        if active and active.get("state") in {"queued", "running", "cancelling"}:
            raise HTTPException(409, "\u5df2\u6709\u6279\u91cf CPA \u5bfc\u51fa\u91cd\u8bd5\u4efb\u52a1\u5728\u8fd0\u884c")

    task_id = _manual_cpa_batch_task_id()
    task: dict[str, Any] = {
        "task_id": task_id,
        "state": "queued",
        "created_at": datetime.now(UTC).isoformat(),
        "started_at": None,
        "finished_at": None,
        "total": len(tokens),
        "current_index": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "cancel_requested": False,
        "message": "\u5df2\u52a0\u5165\u6279\u91cf CPA \u5bfc\u51fa\u91cd\u8bd5\u961f\u5217",
        "child_task_ids": [],
    }
    tasks[task_id] = task
    _set_active_cpa_batch_retry(request, task_id)

    async def _run() -> None:
        individual_tasks, _, _ = _cpa_retry_runtime(request)
        task["state"] = "running"
        task["started_at"] = datetime.now(UTC).isoformat()
        task["message"] = "\u6b63\u5728\u4e32\u884c\u5904\u7406 CPA \u5bfc\u51fa\u91cd\u8bd5"
        try:
            for index, token in enumerate(tokens, start=1):
                if task["cancel_requested"]:
                    task["skipped"] += len(tokens) - index + 1
                    break
                task["current_index"] = index
                try:
                    child = await retry_cpa_auth_for_account(
                        CpaRetryRequest(token=token), request, repo
                    )
                except HTTPException as exc:
                    if exc.status_code == 409:
                        task["skipped"] += 1
                    else:
                        task["failed"] += 1
                    continue
                except Exception:  # noqa: BLE001
                    task["failed"] += 1
                    continue

                child_task_id = str(child.get("task_id") or "")
                if not child_task_id:
                    task["failed"] += 1
                    continue
                task["child_task_ids"].append(child_task_id)
                while True:
                    await asyncio.sleep(1)
                    child_state = individual_tasks.get(child_task_id)
                    if child_state is None:
                        task["failed"] += 1
                        break
                    state = str(child_state.get("state") or "")
                    if state == "completed":
                        task["succeeded"] += 1
                        break
                    if state == "failed":
                        task["failed"] += 1
                        break

            if task["cancel_requested"]:
                task["state"] = "cancelled"
                task["message"] = "\u6279\u91cf CPA \u5bfc\u51fa\u91cd\u8bd5\u5df2\u53d6\u6d88\uff0c\u672a\u5f00\u59cb\u7684\u8d26\u53f7\u5df2\u8df3\u8fc7"
            else:
                task["state"] = "completed"
                task["message"] = "\u6279\u91cf CPA \u5bfc\u51fa\u91cd\u8bd5\u5b8c\u6210"
        except Exception:  # noqa: BLE001
            task["state"] = "failed"
            task["message"] = "\u6279\u91cf CPA \u5bfc\u51fa\u91cd\u8bd5\u5f02\u5e38\u4e2d\u65ad"
        finally:
            task["finished_at"] = datetime.now(UTC).isoformat()
            if getattr(request.app.state, "manual_cpa_batch_retry_active_id", None) == task_id:
                _set_active_cpa_batch_retry(request, None)

    asyncio.create_task(_run(), name=f"manual-cpa-batch-retry-{task_id}")
    return _batch_retry_state_payload(task)


@router.get("/cpa-auths/retry/batch/{batch_id}")
async def get_cpa_batch_retry_status(batch_id: str, request: Request):
    tasks, _ = _cpa_batch_retry_runtime(request)
    task = tasks.get(batch_id)
    if task is None:
        raise HTTPException(404, "\u672a\u627e\u5230\u6279\u91cf CPA \u5bfc\u51fa\u91cd\u8bd5\u4efb\u52a1")
    return _batch_retry_state_payload(task)


@router.post("/cpa-auths/retry/batch/{batch_id}/cancel")
async def cancel_cpa_batch_retry(batch_id: str, request: Request):
    tasks, _ = _cpa_batch_retry_runtime(request)
    task = tasks.get(batch_id)
    if task is None:
        raise HTTPException(404, "\u672a\u627e\u5230\u6279\u91cf CPA \u5bfc\u51fa\u91cd\u8bd5\u4efb\u52a1")
    if task.get("state") not in {"queued", "running", "cancelling"}:
        return _batch_retry_state_payload(task)
    task["cancel_requested"] = True
    if task.get("state") == "queued":
        task["state"] = "cancelling"
    task["message"] = "\u5df2\u8bf7\u6c42\u53d6\u6d88\uff0c\u5f53\u524d\u8d26\u53f7\u5bfc\u51fa\u7ed3\u675f\u540e\u4e0d\u518d\u542f\u52a8\u540e\u7eed\u8d26\u53f7"
    return _batch_retry_state_payload(task)


@router.get("/cpa-auths/retry/{task_id}")
async def get_cpa_retry_status(task_id: str, request: Request):
    tasks, _, _ = _cpa_retry_runtime(request)
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "未找到 CPA 导出重试任务")
    return _retry_state_payload(task)


@router.get("/cpa-auths/tasks/{task_id}/logs")
async def get_cpa_auth_task_logs(task_id: str):
    """Read the latest task log lines; the endpoint never exposes CPA JSON contents."""
    try:
        lines = _read_cpa_task_log(task_id)
    except ValueError as exc:
        raise HTTPException(400, "invalid task_id") from exc
    if not lines:
        raise HTTPException(404, "暂无可查看的任务日志")
    return {"task_id": task_id, "lines": lines}


@router.get("/cpa-auths/tasks")
async def list_cpa_auth_export_tasks(
    request: Request,
    page: int = 1,
    page_size: int = 10,
):
    """Return a page of task-isolated CPA export groups without auth contents."""
    safe_page = max(1, int(page or 1))
    safe_size = min(max(int(page_size or 10), 10), 2_000)
    settings = _manager(request)._read_settings_raw()
    rows = list_cpa_auth_tasks(dict(settings.get("cpa") or {}))
    total = len(rows)
    total_pages = max(1, (total + safe_size - 1) // safe_size)
    current_page = min(safe_page, total_pages)
    start = (current_page - 1) * safe_size
    return {
        "items": rows[start:start + safe_size],
        "total": total,
        "page": current_page,
        "page_size": safe_size,
        "total_pages": total_pages,
    }


@router.get("/cpa-auths/export")
async def export_cpa_auths(task_id: str, request: Request):
    """Download CPA Auth JSON files generated by exactly one registration task."""
    selected_task = str(task_id or "").strip()
    if not selected_task:
        raise HTTPException(400, "task_id is required")
    settings = _manager(request)._read_settings_raw()
    try:
        files = cpa_auth_task_files(dict(settings.get("cpa") or {}), selected_task)
    except ValueError as exc:
        raise HTTPException(400, "invalid task_id") from exc
    if not files:
        raise HTTPException(404, "No CPA Auth JSON files found for this task")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, arcname=path.name)
    safe_name = "".join(char if char.isalnum() or char in "_.-" else "_" for char in selected_task)
    headers = {"Content-Disposition": f'attachment; filename="grok2api-cpa-auths-{safe_name}.zip"'}
    return Response(content=payload.getvalue(), media_type="application/zip", headers=headers)


__all__ = ["router"]
