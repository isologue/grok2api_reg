"""Persistent Grok Build OAuth account pool backed by CPA Auth JSON files."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import secrets
import threading
import time
from urllib.parse import urlencode
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
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


def _now_ms() -> int:
    return int(time.time() * 1000)


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
    enabled: bool = True
    cooldown_until: int = 0
    last_used_at: int = 0
    success_count: int = 0
    failure_count: int = 0
    last_error: str = ""
    last_sync_at: int = 0
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)

    def available_for(self, model: str, now: int) -> bool:
        return self.enabled and self.cooldown_until <= now and model in self.models


@dataclass(slots=True)
class BuildLease:
    account: BuildAccount
    reserved_at: int


class BuildAccountStore:
    def __init__(self) -> None:
        self._path = data_path("build_accounts.json")
        self._accounts: dict[str, BuildAccount] = {}
        self._loaded = False
        # Registration runs in a separate subprocess. Track the persisted file so
        # the web/API process notices a successful auto-import without a restart.
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
                        self._accounts[account.id] = account
            self._file_marker = self._disk_marker()
        except FileNotFoundError:
            self._file_marker = None
        except Exception as exc:
            # Keep a later request eligible to retry the read instead of serving
            # stale data forever after a temporary external writer race.
            self._loaded = False
            logger.warning("build account store load failed: {}", exc)

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "accounts": [item.model_dump() for item in self._accounts.values()]}
        temp = self._path.with_suffix(".tmp")
        temp.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        with contextlib.suppress(OSError):
            os.chmod(temp, 0o600)
        temp.replace(self._path)
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)
        self._file_marker = self._disk_marker()
        self._loaded = True

    def list(self, *, include_secrets: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            self._load_locked()
            result: list[dict[str, Any]] = []
            for item in sorted(self._accounts.values(), key=lambda x: (x.email or x.user_id or x.id).lower()):
                value = item.model_dump()
                value["inflight"] = self._inflight.get(item.id, 0)
                if not include_secrets:
                    value.pop("access_token", None)
                    value.pop("refresh_token", None)
                    value["headers"] = {k: "***" if "authorization" in k.lower() else v for k, v in item.headers.items()}
                result.append(value)
            return result

    def has_model(self, model: str) -> bool:
        now = _now_ms()
        with self._lock:
            self._load_locked()
            return any(item.available_for(model, now) for item in self._accounts.values())

    def known_models(self) -> list[str]:
        with self._lock:
            self._load_locked()
            return sorted({model for item in self._accounts.values() if item.enabled for model in item.models})

    def get(self, account_id: str) -> BuildAccount | None:
        with self._lock:
            self._load_locked()
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
                    headers.update({str(k): str(v) for k, v in (payload.get("headers") or {}).items() if isinstance(k, str) and isinstance(v, (str, int, float, bool))})
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
                        enabled=not bool(payload.get("disabled", False)),
                        cooldown_until=0,
                        last_used_at=previous.last_used_at if previous else 0,
                        success_count=previous.success_count if previous else 0,
                        failure_count=previous.failure_count if previous else 0,
                        last_error="",
                        last_sync_at=previous.last_sync_at if previous else 0,
                        created_at=previous.created_at if previous else _now_ms(),
                        updated_at=_now_ms(),
                    )
                    self._accounts[account_id] = account
                    if previous is None:
                        added += 1
                    else:
                        updated += 1
                except Exception as exc:
                    # Do not log the payload: it contains OAuth credentials.
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
            candidates = [
                item for item in self._accounts.values()
                if item.id not in excluded and item.available_for(model, now)
            ]
            if not candidates:
                raise RateLimitError(f"No available Grok Build account supports {model}")
            item = min(candidates, key=lambda x: (self._inflight.get(x.id, 0), x.last_used_at, x.id))
            self._inflight[item.id] = self._inflight.get(item.id, 0) + 1
            return BuildLease(account=item.model_copy(deep=True), reserved_at=now)

    def release(self, lease: BuildLease, *, success: bool, error: BaseException | None = None) -> None:
        with self._lock:
            self._load_locked()
            self._inflight[lease.account.id] = max(0, self._inflight.get(lease.account.id, 1) - 1)
            item = self._accounts.get(lease.account.id)
            if item is None:
                return
            item.last_used_at = _now_ms()
            if success:
                item.success_count += 1
                item.last_error = ""
            else:
                item.failure_count += 1
                if error is not None:
                    item.last_error = str(error)[:500]
                    status = getattr(error, "status", None)
                    if status in {401, 403}:
                        item.cooldown_until = _now_ms() + 60_000
            item.updated_at = _now_ms()
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


async def refresh_account(account: BuildAccount) -> BuildAccount:
    if account.expires_at and account.expires_at > _now_ms() + 120_000:
        return account
    lock = store.refresh_lock(account.id)
    async with lock:
        current = store.get(account.id)
        if current is None:
            raise RateLimitError("Grok Build account was removed")
        if current.expires_at and current.expires_at > _now_ms() + 120_000:
            return current
        proxy = await get_proxy_runtime()
        lease = await proxy.acquire()
        kwargs = build_session_kwargs(lease=lease)
        payload = {"grant_type": "refresh_token", "refresh_token": current.refresh_token, "client_id": current.client_id}
        async with ResettableSession(**kwargs) as session:
            try:
                response = await session.post(current.token_endpoint, data=payload, timeout=30.0)
            except Exception as exc:
                raise UpstreamError(f"Grok Build OAuth refresh transport failed: {exc}", status=502) from exc
        body = response.content.decode("utf-8", "replace")
        if response.status_code != 200:
            raise UpstreamError("Grok Build OAuth refresh failed", status=response.status_code, body=body[:600])
        data = orjson.loads(response.content)
        current.access_token = str(data.get("access_token") or "").strip()
        current.refresh_token = str(data.get("refresh_token") or current.refresh_token).strip()
        if not current.access_token:
            raise UpstreamError("Grok Build OAuth refresh returned no access token", status=502)
        expires_in = int(data.get("expires_in") or 21600)
        current.expires_at = _now_ms() + max(60, expires_in) * 1000
        store.update(current)
        return current


__all__ = ["BuildAccount", "BuildLease", "DEFAULT_BASE_URL", "DEFAULT_HEADERS", "store", "refresh_account", "import_cpa_auth_file"]
