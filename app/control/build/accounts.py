"""Persistent Grok Build OAuth account pool backed by CPA Auth JSON files."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import threading
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel, Field

from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
from app.platform.config.snapshot import get_config
from app.platform.errors import RateLimitError, UpstreamError, ValidationError
from app.platform.logging.logger import logger
from app.platform.paths import data_path

DEFAULT_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
DEFAULT_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
DEFAULT_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
DEFAULT_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}

BUILD_STATUS_ACTIVE = "active"
BUILD_STATUS_COOLING = "cooling"
BUILD_STATUS_DISABLED = "disabled"
BUILD_STATUS_MANUAL_DISABLED = "manual_disabled"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _config_seconds(key: str, default: int, *, maximum: int = 7 * 86_400) -> int:
    try:
        return min(max(1, int(get_config(key, default))), maximum)
    except (TypeError, ValueError):
        return default


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        value = json.loads(base64.urlsafe_b64decode(part))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _expiry_ms(value: Any, access_token: str) -> int:
    raw = str(value or "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    claims = _decode_jwt_claims(access_token)
    with contextlib.suppress(KeyError, TypeError, ValueError):
        return int(claims["exp"]) * 1000
    return 0


def _identity(payload: dict[str, Any], access_token: str, refresh_token: str) -> tuple[str, str, str]:
    claims = _decode_jwt_claims(access_token)
    email = str(payload.get("email") or claims.get("email") or "").strip()
    user_id = str(payload.get("user_id") or payload.get("sub") or payload.get("principal_id") or claims.get("sub") or "").strip()
    key = user_id or email or hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()[:24]
    return key, email, user_id


def _error_body(error: BaseException) -> str:
    details = getattr(error, "details", None)
    if not isinstance(details, dict):
        return ""
    return str(details.get("body") or "")


def _error_details(error: BaseException) -> dict[str, Any]:
    details = getattr(error, "details", None)
    return details if isinstance(details, dict) else {}


def _is_oauth_refresh_error(error: BaseException) -> bool:
    details = _error_details(error)
    return bool(details.get("build_oauth_refresh")) or "grok build oauth refresh" in str(error).lower()


def _is_invalid_oauth_refresh(error: BaseException) -> bool:
    text = f"{error} {_error_body(error)}".lower()
    markers = (
        "invalid_grant",
        "invalid refresh token",
        "refresh token has expired",
        "refresh token revoked",
        "token revoked",
        "refresh token is invalid",
        "refresh_token_invalid",
        "invalid_token",
    )
    return any(marker in text for marker in markers)


def _retry_after_ms(error: BaseException, now: int) -> int:
    details = _error_details(error)
    raw_ms = details.get("retry_after_ms")
    with contextlib.suppress(TypeError, ValueError):
        value = int(raw_ms)
        if value > 0:
            return value
    raw = str(details.get("retry_after") or "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            return max(1, int(float(raw) * 1000))
        with contextlib.suppress(TypeError, ValueError):
            target = int(parsedate_to_datetime(raw).timestamp() * 1000)
            if target > now:
                return target - now
    return 0


def _safe_error_summary(error: BaseException) -> str:
    status = getattr(error, "status", None)
    if _is_invalid_oauth_refresh(error):
        return "OAuth refresh token is invalid or revoked"
    if _is_oauth_refresh_error(error):
        return f"Grok Build OAuth refresh failed{f' (HTTP {status})' if status else ''}"
    if status == 429:
        return "Grok Build upstream rate limited this account"
    if status in {401, 403}:
        return f"Grok Build authorization failed (HTTP {status})"
    text = str(error).replace("\n", " ").strip()
    return text[:500] or "Grok Build request failed"


class BuildAccount(BaseModel):
    id: str
    source_key: str
    email: str = ""
    user_id: str = ""
    access_token: str
    refresh_token: str
    expires_at: int = 0
    base_url: str = DEFAULT_BASE_URL
    token_endpoint: str = DEFAULT_TOKEN_ENDPOINT
    client_id: str = DEFAULT_CLIENT_ID
    headers: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_HEADERS))
    models: list[str] = Field(default_factory=list)
    # Manual operator switch. It is intentionally independent from runtime_status.
    enabled: bool = True
    runtime_status: str = BUILD_STATUS_ACTIVE
    state_reason: str = ""
    cooldown_until: int = 0
    cooldown_reason: str = ""
    disabled_at: int = 0
    last_used_at: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_status_code: int = 0
    last_error: str = ""
    last_sync_at: int = 0
    last_refresh_at: int = 0
    last_refresh_error: str = ""
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)

    def effective_status(self, now: int) -> str:
        if not self.enabled:
            return BUILD_STATUS_MANUAL_DISABLED
        if self.runtime_status == BUILD_STATUS_COOLING and self.cooldown_until > now:
            return BUILD_STATUS_COOLING
        if self.runtime_status == BUILD_STATUS_DISABLED:
            return BUILD_STATUS_DISABLED
        return BUILD_STATUS_ACTIVE

    def available_for(self, model: str, now: int) -> bool:
        return self.effective_status(now) == BUILD_STATUS_ACTIVE and model in self.models


@dataclass(slots=True)
class BuildLease:
    account: BuildAccount
    reserved_at: int


class BuildAccountStore:
    def __init__(self) -> None:
        self._path = data_path("build_accounts.json")
        self._accounts: dict[str, BuildAccount] = {}
        self._loaded = False
        self._file_marker: tuple[int, int] | None = None
        self._lock = threading.RLock()
        self._inflight: dict[str, int] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def _disk_marker(self) -> tuple[int, int] | None:
        try:
            stat = self._path.stat()
            return stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return None
        except OSError:
            return self._file_marker

    def _load_locked(self) -> None:
        marker = self._disk_marker()
        if self._loaded and marker == self._file_marker:
            return
        self._loaded = True
        self._accounts.clear()
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            values = raw.get("accounts") if isinstance(raw, dict) else raw
            if isinstance(values, list):
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    with contextlib.suppress(Exception):
                        account = BuildAccount.model_validate(item)
                        if account.runtime_status not in {BUILD_STATUS_ACTIVE, BUILD_STATUS_COOLING, BUILD_STATUS_DISABLED}:
                            account.runtime_status = BUILD_STATUS_ACTIVE
                        self._accounts[account.id] = account
            self._file_marker = self._disk_marker()
        except FileNotFoundError:
            self._file_marker = None
        except Exception as exc:
            self._loaded = False
            logger.warning("build account store load failed: {}", exc)

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 2, "accounts": [item.model_dump() for item in self._accounts.values()]}
        temp = self._path.with_suffix(".tmp")
        temp.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        with contextlib.suppress(OSError):
            os.chmod(temp, 0o600)
        temp.replace(self._path)
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)
        self._file_marker = self._disk_marker()
        self._loaded = True

    def _recover_expired_cooldowns_locked(self, now: int) -> bool:
        changed = False
        for item in self._accounts.values():
            if item.runtime_status != BUILD_STATUS_COOLING or item.cooldown_until <= 0 or item.cooldown_until > now:
                continue
            item.runtime_status = BUILD_STATUS_ACTIVE
            item.state_reason = ""
            item.cooldown_reason = ""
            item.cooldown_until = 0
            item.updated_at = now
            changed = True
        return changed

    def _public_item_locked(self, item: BuildAccount, now: int, *, include_secrets: bool) -> dict[str, Any]:
        value = item.model_dump()
        value["status"] = item.effective_status(now)
        value["cooldown_remaining_ms"] = max(0, item.cooldown_until - now) if value["status"] == BUILD_STATUS_COOLING else 0
        value["inflight"] = self._inflight.get(item.id, 0)
        value["available"] = value["status"] == BUILD_STATUS_ACTIVE and bool(item.models)
        if not include_secrets:
            # CPA headers are not needed by the administrator UI and may contain
            # user-provided credentials.  Do not expose them together with the
            # list/detail API, even when a header name does not say Authorization.
            value.pop("access_token", None)
            value.pop("refresh_token", None)
            value.pop("headers", None)
        return value

    def list(self, *, include_secrets: bool = False) -> list[dict[str, Any]]:
        now = _now_ms()
        with self._lock:
            self._load_locked()
            if self._recover_expired_cooldowns_locked(now):
                self._save_locked()
            return [
                self._public_item_locked(item, now, include_secrets=include_secrets)
                for item in sorted(self._accounts.values(), key=lambda value: (value.email or value.user_id or value.id).lower())
            ]

    def has_model(self, model: str) -> bool:
        now = _now_ms()
        with self._lock:
            self._load_locked()
            if self._recover_expired_cooldowns_locked(now):
                self._save_locked()
            return any(item.available_for(model, now) for item in self._accounts.values())

    def known_models(self) -> list[str]:
        now = _now_ms()
        with self._lock:
            self._load_locked()
            if self._recover_expired_cooldowns_locked(now):
                self._save_locked()
            return sorted({model for item in self._accounts.values() if item.effective_status(now) == BUILD_STATUS_ACTIVE for model in item.models})

    def get(self, account_id: str) -> BuildAccount | None:
        now = _now_ms()
        with self._lock:
            self._load_locked()
            if self._recover_expired_cooldowns_locked(now):
                self._save_locked()
            item = self._accounts.get(account_id)
            return item.model_copy(deep=True) if item else None

    def upsert_payloads(self, payloads: list[dict[str, Any]], *, model_overrides: dict[str, list[str]] | None = None) -> dict[str, int]:
        added = updated = skipped = 0
        with self._lock:
            self._load_locked()
            for payload in payloads:
                try:
                    access_token = str(payload.get("access_token") or "").strip()
                    refresh_token = str(payload.get("refresh_token") or "").strip()
                    if not access_token or not refresh_token:
                        raise ValidationError("CPA OAuth JSON requires access_token and refresh_token")
                    if str(payload.get("auth_kind") or "oauth").lower() != "oauth":
                        raise ValidationError("only OAuth CPA Auth JSON is supported")
                    source_key, email, user_id = _identity(payload, access_token, refresh_token)
                    account_id = hashlib.sha256(("build:" + source_key).encode("utf-8")).hexdigest()[:24]
                    headers = dict(DEFAULT_HEADERS)
                    headers.update({str(key): str(value) for key, value in (payload.get("headers") or {}).items() if isinstance(key, str) and isinstance(value, (str, int, float, bool))})
                    previous = self._accounts.get(account_id)
                    models = list((model_overrides or {}).get(source_key) or (previous.models if previous else []))
                    account = BuildAccount(
                        id=account_id,
                        source_key=source_key,
                        email=email,
                        user_id=user_id,
                        access_token=access_token,
                        refresh_token=refresh_token,
                        expires_at=_expiry_ms(payload.get("expired") or payload.get("expires_at"), access_token),
                        base_url=str(payload.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
                        token_endpoint=str(payload.get("token_endpoint") or DEFAULT_TOKEN_ENDPOINT),
                        client_id=str(payload.get("client_id") or payload.get("oidc_client_id") or DEFAULT_CLIENT_ID),
                        headers=headers,
                        models=models,
                        # Importing a refreshed CPA Auth repairs runtime OAuth state,
                        # but must not silently override an administrator's manual stop.
                        enabled=False if bool(payload.get("disabled", False)) else (previous.enabled if previous else True),
                        runtime_status=BUILD_STATUS_ACTIVE,
                        state_reason="",
                        cooldown_until=0,
                        cooldown_reason="",
                        disabled_at=0,
                        last_used_at=previous.last_used_at if previous else 0,
                        success_count=previous.success_count if previous else 0,
                        failure_count=previous.failure_count if previous else 0,
                        last_status_code=0,
                        last_error="",
                        last_sync_at=previous.last_sync_at if previous else 0,
                        last_refresh_at=previous.last_refresh_at if previous else 0,
                        last_refresh_error="",
                        created_at=previous.created_at if previous else _now_ms(),
                        updated_at=_now_ms(),
                    )
                    self._accounts[account_id] = account
                    if previous is None:
                        added += 1
                    else:
                        updated += 1
                except Exception as exc:
                    logger.warning("Build CPA Auth import skipped: {}", exc)
                    skipped += 1
            if added or updated:
                self._save_locked()
        return {"added": added, "updated": updated, "skipped": skipped}

    def import_bytes(self, data: bytes) -> tuple[dict[str, int], list[str]]:
        entries: list[dict[str, Any]] = []
        names: list[str] = []
        if data[:4] == b"PK\x03\x04":
            with zipfile.ZipFile(BytesIO(data)) as archive:
                for info in archive.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".json") or info.file_size > 2 * 1024 * 1024:
                        continue
                    with contextlib.suppress(Exception):
                        value = json.loads(archive.read(info))
                        if isinstance(value, dict):
                            entries.append(value)
                            names.append(info.filename)
        else:
            value = json.loads(data)
            if isinstance(value, dict) and isinstance(value.get("accounts"), list):
                entries = [item for item in value["accounts"] if isinstance(item, dict)]
            elif isinstance(value, dict):
                entries = [value]
        if not entries:
            raise ValidationError("No valid CPA OAuth JSON entries found")
        return self.upsert_payloads(entries), names

    def reserve(self, model: str, *, exclude_ids: set[str] | None = None) -> BuildLease:
        now = _now_ms()
        excluded = exclude_ids or set()
        with self._lock:
            self._load_locked()
            if self._recover_expired_cooldowns_locked(now):
                self._save_locked()
            candidates = [item for item in self._accounts.values() if item.id not in excluded and item.available_for(model, now)]
            if not candidates:
                raise RateLimitError(f"No available Grok Build account supports {model}")
            item = min(candidates, key=lambda value: (self._inflight.get(value.id, 0), value.last_used_at, value.id))
            self._inflight[item.id] = self._inflight.get(item.id, 0) + 1
            return BuildLease(account=item.model_copy(deep=True), reserved_at=now)

    def _apply_failure_locked(self, item: BuildAccount, error: BaseException, now: int, *, count_failure: bool) -> None:
        if count_failure:
            item.failure_count += 1
        status = getattr(error, "status", None)
        item.last_status_code = int(status) if isinstance(status, int) else 0
        item.last_error = _safe_error_summary(error)
        if _is_oauth_refresh_error(error):
            item.last_refresh_at = now
            item.last_refresh_error = item.last_error

        if _is_invalid_oauth_refresh(error) or (_is_oauth_refresh_error(error) and status in {400, 401, 403}):
            item.runtime_status = BUILD_STATUS_DISABLED
            item.state_reason = "oauth_refresh_token_invalid" if _is_invalid_oauth_refresh(error) else "oauth_refresh_failed"
            item.cooldown_until = 0
            item.cooldown_reason = ""
            item.disabled_at = now
        elif status == 429:
            delay = _retry_after_ms(error, now) or _config_seconds("build.rate_limit_cooling_sec", 900)
            item.runtime_status = BUILD_STATUS_COOLING
            item.state_reason = "rate_limited"
            item.cooldown_reason = "rate_limited"
            item.cooldown_until = now + delay
        elif status in {401, 403} and bool(_error_details(error).get("build_auth_rechecked")):
            item.runtime_status = BUILD_STATUS_DISABLED
            item.state_reason = "build_authorization_rejected"
            item.cooldown_until = 0
            item.cooldown_reason = ""
            item.disabled_at = now
        elif status in {401, 403, 408, 409, 425, 500, 502, 503, 504}:
            item.runtime_status = BUILD_STATUS_COOLING
            item.state_reason = "transient_upstream_failure"
            item.cooldown_reason = "transient_upstream_failure"
            item.cooldown_until = now + _config_seconds("build.transient_error_cooling_sec", 60)
        item.updated_at = now

    def record_failure(self, account_id: str, error: BaseException, *, count_failure: bool = True) -> BuildAccount | None:
        with self._lock:
            self._load_locked()
            item = self._accounts.get(account_id)
            if item is None:
                return None
            self._apply_failure_locked(item, error, _now_ms(), count_failure=count_failure)
            self._save_locked()
            return item.model_copy(deep=True)

    def mark_verified(self, account_id: str, models: list[str]) -> BuildAccount:
        with self._lock:
            self._load_locked()
            item = self._accounts.get(account_id)
            if item is None:
                raise ValidationError("Build account not found")
            now = _now_ms()
            item.models = sorted({str(model).strip() for model in models if str(model).strip()})
            item.runtime_status = BUILD_STATUS_ACTIVE
            item.state_reason = ""
            item.cooldown_until = 0
            item.cooldown_reason = ""
            item.disabled_at = 0
            item.last_error = ""
            item.last_status_code = 0
            item.last_sync_at = now
            item.updated_at = now
            self._save_locked()
            return item.model_copy(deep=True)

    def release(self, lease: BuildLease, *, success: bool, error: BaseException | None = None) -> None:
        with self._lock:
            self._load_locked()
            self._inflight[lease.account.id] = max(0, self._inflight.get(lease.account.id, 1) - 1)
            item = self._accounts.get(lease.account.id)
            if item is None:
                return
            now = _now_ms()
            item.last_used_at = now
            if success:
                item.success_count += 1
                item.last_error = ""
                item.last_status_code = 0
                # A successful concurrent request must not prematurely clear a
                # persisted 429 limit.  Only a non-rate-limit transient cooldown
                # can be cleared by later success; rate limits recover on deadline.
                if item.runtime_status == BUILD_STATUS_COOLING and item.cooldown_reason != "rate_limited":
                    item.runtime_status = BUILD_STATUS_ACTIVE
                    item.state_reason = ""
                    item.cooldown_until = 0
                    item.cooldown_reason = ""
                item.updated_at = now
            elif error is not None:
                self._apply_failure_locked(item, error, now, count_failure=True)
            else:
                item.failure_count += 1
                item.last_error = "Grok Build request failed"
                item.updated_at = now
            self._save_locked()

    def update(self, account: BuildAccount) -> None:
        with self._lock:
            self._load_locked()
            account.updated_at = _now_ms()
            self._accounts[account.id] = account
            self._save_locked()

    def delete(self, ids: list[str]) -> int:
        with self._lock:
            self._load_locked()
            count = sum(1 for item in ids if self._accounts.pop(item, None) is not None)
            if count:
                self._save_locked()
            return count

    def set_enabled(self, account_id: str, enabled: bool) -> BuildAccount:
        with self._lock:
            self._load_locked()
            item = self._accounts.get(account_id)
            if item is None:
                raise ValidationError("Build account not found")
            item.enabled = enabled
            item.updated_at = _now_ms()
            self._save_locked()
            return item.model_copy(deep=True)

    def refresh_lock(self, account_id: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(account_id)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[account_id] = lock
        return lock


store = BuildAccountStore()


def import_cpa_auth_file(path: str | Path, *, model_ids: list[str] | None = None) -> dict[str, int]:
    """Synchronous bridge used by the registration worker after CPA mint succeeds."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("CPA auth file must contain an object")
    access_token = str(value.get("access_token") or "")
    refresh_token = str(value.get("refresh_token") or "")
    key, _email, _user = _identity(value, access_token, refresh_token)
    return store.upsert_payloads([value], model_overrides={key: list(model_ids or [])})


def _with_retry_after(error: UpstreamError, response: Any) -> UpstreamError:
    headers = getattr(response, "headers", {}) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw:
        error.details["retry_after"] = str(raw)
    error.details["build_oauth_refresh"] = True
    return error


async def refresh_account(account: BuildAccount, *, force: bool = False) -> BuildAccount:
    leeway_ms = _config_seconds("build.oauth_refresh_leeway_sec", 120, maximum=3_600) * 1000
    if not force and account.expires_at and account.expires_at > _now_ms() + leeway_ms:
        return account
    lock = store.refresh_lock(account.id)
    async with lock:
        current = store.get(account.id)
        if current is None:
            raise RateLimitError("Grok Build account was removed")
        if not force and current.expires_at and current.expires_at > _now_ms() + leeway_ms:
            return current
        proxy = await get_proxy_runtime()
        lease = await proxy.acquire()
        kwargs = build_session_kwargs(lease=lease)
        payload = {"grant_type": "refresh_token", "refresh_token": current.refresh_token, "client_id": current.client_id}
        async with ResettableSession(**kwargs) as session:
            try:
                response = await session.post(current.token_endpoint, data=payload, timeout=30.0)
            except Exception as exc:
                error = UpstreamError(f"Grok Build OAuth refresh transport failed: {exc}", status=502)
                error.details["build_oauth_refresh"] = True
                raise error from exc
        body = response.content.decode("utf-8", "replace")
        if response.status_code != 200:
            raise _with_retry_after(UpstreamError("Grok Build OAuth refresh failed", status=response.status_code, body=body[:600]), response)
        try:
            data = orjson.loads(response.content)
        except orjson.JSONDecodeError as exc:
            error = UpstreamError("Grok Build OAuth refresh returned invalid JSON", status=502, body=body[:600])
            error.details["build_oauth_refresh"] = True
            raise error from exc
        current.access_token = str(data.get("access_token") or "").strip()
        current.refresh_token = str(data.get("refresh_token") or current.refresh_token).strip()
        if not current.access_token:
            error = UpstreamError("Grok Build OAuth refresh returned no access token", status=502)
            error.details["build_oauth_refresh"] = True
            raise error
        try:
            expires_in = int(data.get("expires_in") or 21600)
        except (TypeError, ValueError):
            expires_in = 21600
        current.expires_at = _now_ms() + max(60, expires_in) * 1000
        current.last_refresh_at = _now_ms()
        current.last_refresh_error = ""
        store.update(current)
        return current


__all__ = [
    "BuildAccount", "BuildLease", "BUILD_STATUS_ACTIVE", "BUILD_STATUS_COOLING",
    "BUILD_STATUS_DISABLED", "BUILD_STATUS_MANUAL_DISABLED", "DEFAULT_BASE_URL",
    "DEFAULT_HEADERS", "store", "refresh_account", "import_cpa_auth_file",
]
