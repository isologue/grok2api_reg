"""Mailbox providers used by the integrated browser registration worker."""
from __future__ import annotations

import imaplib
import json
import random
import threading
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from datetime import UTC, datetime
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - Docker image supplies curl_cffi
    curl_requests = None

import requests

from app.platform.paths import data_path


class VerificationCodeTimeout(RuntimeError):
    """The current mailbox did not receive an xAI verification code in time."""


class MailboxRateLimited(RuntimeError):
    """A mailbox API asked the worker to stop polling for a short period."""

    def __init__(self, provider_name: str, retry_after: float | None = None) -> None:
        self.provider_name = provider_name
        self.retry_after = max(0.0, float(retry_after or 0.0))
        wait_text = f"; retry after {self.retry_after:.0f}s" if self.retry_after else ""
        super().__init__(f"{provider_name} inbox rate limited{wait_text}")


@dataclass(frozen=True)
class Mailbox:
    address: str
    provider_index: int
    provider_token: str = ""

    def token(self) -> str:
        return json.dumps({"provider_index": self.provider_index, "provider_token": self.provider_token}, separators=(",", ":"))


class _MailboxProvider:
    name: str
    poll_interval_seconds: float = 3.0

    def create_mailbox(self) -> str | dict[str, str]:
        raise NotImplementedError

    def list_messages(self, address: str, provider_token: str = "") -> list[dict[str, Any]]:
        raise NotImplementedError

    def message_content(self, message_id: str, provider_token: str = "") -> str:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def _create_session(proxy: str = ""):
    session = curl_requests.Session(impersonate="chrome136") if curl_requests else requests.Session()
    if proxy.strip():
        url = proxy.strip() if "://" in proxy else f"http://{proxy.strip()}"
        session.proxies.update({"http": url, "https": url})
    return session


class GptMailProvider(_MailboxProvider):
    """Adapter for the GptMail-compatible API used by grok-register-main."""

    def __init__(self, entry: dict[str, Any], proxy: str = "") -> None:
        self.name = str(entry.get("name") or "GptMail")
        self.base_url = str(entry.get("api_base") or "").rstrip("/")
        self.api_key = str(entry.get("api_key") or "")
        if not self.base_url or not self.api_key:
            raise RuntimeError(f"Mailbox provider {self.name} requires an API base URL and API key")
        self.session = _create_session(proxy)

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json, text/plain, */*", "X-API-Key": self.api_key, "Origin": self.base_url, "Referer": f"{self.base_url}/"}

    def create_mailbox(self) -> str:
        response = self.session.get(f"{self.base_url}/api/generate-email", headers=self._headers(), timeout=20)
        response.raise_for_status()
        try:
            body = response.json()
        except Exception as exc:
            preview = str(getattr(response, "text", "") or "").replace("\n", " ").strip()[:240]
            raise RuntimeError(f"GptMail generate-email returned invalid JSON: HTTP {response.status_code}, url={self.base_url}/api/generate-email, body={preview}") from exc
        address = str((body.get("data") or {}).get("email") or "").strip()
        if not body.get("success") or not address:
            raise RuntimeError(str(body.get("error") or f"{self.name} did not return an email address"))
        return address

    def list_messages(self, address: str, provider_token: str = "") -> list[dict[str, Any]]:
        response = self.session.get(f"{self.base_url}/api/emails", params={"email": address}, headers=self._headers(), timeout=30)
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            return []
        messages = (body.get("data") or {}).get("emails") or []
        return [item for item in messages if isinstance(item, dict)]

    def message_content(self, message_id: str, provider_token: str = "") -> str:
        response = self.session.get(f"{self.base_url}/api/email/{message_id}", headers=self._headers(), timeout=30)
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            return ""
        data = body.get("data") or {}
        return str(data.get("content") or data.get("html_content") or "")

    def close(self) -> None:
        self.session.close()


class TempMailLolProvider(_MailboxProvider):
    """TempMail.lol v2 inbox API, modelled after chatgpt2api's provider."""

    def __init__(self, entry: dict[str, Any], proxy: str = "") -> None:
        self.name = str(entry.get("name") or "TempMail.lol")
        self.base_url = str(entry.get("api_base") or "https://api.tempmail.lol/v2").rstrip("/")
        self.api_key = str(entry.get("api_key") or "").strip()
        raw_domains = entry.get("domains", entry.get("domain", []))
        if isinstance(raw_domains, str):
            raw_domains = [part.strip() for part in raw_domains.split(",")]
        self.domains = [str(item).strip() for item in (raw_domains or []) if str(item).strip()]
        # All mailbox providers consistently use the configured mailbox API proxy.
        self.session = _create_session(proxy)
        # TempMail.lol limits polling more aggressively than GptMail.  Keep this
        # conservative even before a 429 is observed.
        self.poll_interval_seconds = 10.0
        self._inbox_cooldown_until = 0.0
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    @staticmethod
    def _random_prefix() -> str:
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        return "grok" + "".join(secrets.choice(alphabet) for _ in range(10))

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None, expected: tuple[int, ...] = (200,)) -> dict[str, Any]:
        # The service applies the limit per egress IP.  Honour a previously
        # received cooldown rather than immediately issuing another request.
        if path == "/inbox":
            remaining = self._inbox_cooldown_until - time.monotonic()
            if remaining > 0:
                raise MailboxRateLimited(self.name, remaining)

        response = self.session.request(method.upper(), f"{self.base_url}{path}", params=params, json=payload, timeout=30, verify=False)
        if response.status_code == 429:
            retry_after = 0.0
            raw_retry_after = str(response.headers.get("Retry-After") or "").strip()
            try:
                retry_after = float(raw_retry_after)
            except ValueError:
                # TempMail.lol does not always provide Retry-After.  A 15s
                # default prevents the former 3-second retry storm.
                retry_after = 15.0
            retry_after = min(max(retry_after, 10.0), 60.0)
            if path == "/inbox":
                self._inbox_cooldown_until = max(self._inbox_cooldown_until, time.monotonic() + retry_after)
            raise MailboxRateLimited(self.name, retry_after)
        if response.status_code not in expected:
            body = str(getattr(response, "text", "") or "").replace("\n", " ").strip()[:240]
            endpoint = f"{self.base_url}{path}"
            raise RuntimeError(f"TempMail.lol request failed: {method} {endpoint}, HTTP {response.status_code}{f', body={body}' if body else ''}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"TempMail.lol {method} {path} returned a non-object payload")
        return data

    def create_mailbox(self) -> dict[str, str]:
        payload: dict[str, Any] = {}
        if self.domains:
            domain = random.choice(self.domains).lower()
            if domain.startswith("*."):
                domain = f"{self._random_prefix()}.{domain[2:]}"
                payload["prefix"] = self._random_prefix()
            payload["domain"] = domain
        data = self._request("POST", "/inbox/create", payload=payload, expected=(200, 201))
        address = str(data.get("address") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address or not token:
            raise RuntimeError("TempMail.lol response is missing address or token")
        return {"address": address, "token": token}

    @staticmethod
    def _message_id(item: dict[str, Any]) -> str:
        return str(item.get("id") or item.get("token") or item.get("message_id") or "")

    def list_messages(self, address: str, provider_token: str = "") -> list[dict[str, Any]]:
        if not provider_token:
            raise RuntimeError("TempMail.lol mailbox context is missing an inbox token")
        data = self._request("GET", "/inbox", params={"token": provider_token})
        items = data.get("emails") or data.get("messages") or []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def message_content(self, message_id: str, provider_token: str = "") -> str:
        for item in self.list_messages("", provider_token):
            if self._message_id(item) != message_id:
                continue
            values: list[str] = []
            for key in ("content", "text", "text_content", "body", "html", "html_content"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    values.append(value)
                elif isinstance(value, dict):
                    values.extend(str(part) for part in value.values() if isinstance(part, str) and part)
            return "\n".join(values)
        return ""

    def close(self) -> None:
        self.session.close()


# Consumer Microsoft accounts can accept a refresh token on /consumers while
# rejecting the same request against /common.  Keep the compatibility order used
# by the standalone registration worker before using the generic endpoint.
OUTLOOK_TOKEN_URLS = (
    "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    "https://login.microsoftonline.com/common/oauth2/v2.0/token",
)
OUTLOOK_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
OUTLOOK_REST_MESSAGES_URL = "https://outlook.office.com/api/v2.0/me/messages"
OUTLOOK_GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
OUTLOOK_IMAP_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
OUTLOOK_DEFAULT_IMAP_HOST = "outlook.office365.com"
OUTLOOK_IN_USE_STALE_SECONDS = 3600
_OUTLOOK_STATE_LOCK = threading.Lock()


def parse_outlook_credentials(value: str) -> list[dict[str, str]]:
    """Parse one credential per line: email----password----client_id----refresh_token."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in str(value or "").splitlines():
        parts = [part.strip().replace("\ufeff", "") for part in raw.split("----", 3)]
        if len(parts) != 4:
            continue
        email, password, client_id, refresh_token = parts
        key = email.lower()
        if "@" not in email or not client_id or not refresh_token or key in seen:
            continue
        seen.add(key)
        result.append({"email": email, "password": password, "client_id": client_id, "refresh_token": refresh_token})
    return result


def _outlook_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _outlook_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(maximum, int(value)))
    except (TypeError, ValueError):
        return default


def outlook_alias_supported(email: str) -> bool:
    domain = str(email or "").strip().lower().partition("@")[2]
    return domain in {"outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.cn", "hotmail.co.uk"}


def outlook_alias_address(email: str, tag: str) -> str:
    local, sep, domain = str(email or "").strip().partition("@")
    if not sep:
        return email
    return f"{local.split('+', 1)[0]}+{tag}@{domain}"


def expand_outlook_aliases(credentials: list[dict[str, str]], entry: dict[str, Any] | None = None) -> list[dict[str, str]]:
    source = entry or {}
    enabled = _outlook_bool(source.get("alias_enabled"), False)
    per_email = _outlook_int(source.get("alias_per_email"), 0, 0, 200)
    include_original = _outlook_bool(source.get("alias_include_original"), True)
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "", str(source.get("alias_prefix") or "c2api").strip()) or "c2api"
    if not enabled or per_email <= 0:
        return [dict(item) for item in credentials]
    expanded: list[dict[str, str]] = []
    seen: set[str] = set()
    for credential in credentials:
        original = str(credential.get("email") or "").strip()
        if include_original and original and original.lower() not in seen:
            expanded.append(dict(credential))
            seen.add(original.lower())
        if not outlook_alias_supported(original):
            continue
        for index in range(1, per_email + 1):
            alias = outlook_alias_address(original, f"{prefix}{index}")
            if alias.lower() in seen:
                continue
            expanded.append({**credential, "email": alias, "login_email": original, "alias_of": original})
            seen.add(alias.lower())
    return expanded


def _outlook_state_path():
    return data_path("registration/outlook_mailbox_states.json")


def _read_outlook_state() -> dict[str, dict[str, str]]:
    try:
        value = json.loads(_outlook_state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_outlook_state(state: dict[str, dict[str, str]]) -> None:
    path = _outlook_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: state[key] for key in sorted(state)}, ensure_ascii=False, indent=2), encoding="utf-8")


def _outlook_available(state: dict[str, dict[str, str]], email: str) -> bool:
    entry = state.get(email.lower())
    if not isinstance(entry, dict):
        return True
    current = str(entry.get("state") or "")
    if current == "in_use":
        try:
            updated = datetime.fromisoformat(str(entry.get("updated_at") or ""))
            return (datetime.now(UTC) - updated).total_seconds() >= OUTLOOK_IN_USE_STALE_SECONDS
        except ValueError:
            return False
    return current not in {"used", "failed", "login_required", "token_invalid"}


def outlook_pool_stats(credentials: list[dict[str, str]], entry: dict[str, Any] | None = None) -> dict[str, int]:
    pool = expand_outlook_aliases(credentials, entry)
    with _OUTLOOK_STATE_LOCK:
        state = _read_outlook_state()
    counts = {"available": 0, "in_use": 0, "used": 0, "failed": 0, "login_required": 0, "token_invalid": 0}
    for item in pool:
        email = str(item.get("email") or "").lower()
        status = str((state.get(email) or {}).get("state") or "available")
        if status == "in_use" and _outlook_available(state, email):
            status = "available"
        if status not in counts:
            status = "available"
        counts[status] += 1
    counts["retryable"] = counts["failed"]
    counts["invalid"] = counts["login_required"] + counts["token_invalid"]
    counts["abnormal"] = counts["retryable"] + counts["invalid"]
    counts["saved"] = len(pool)
    return counts


def outlook_pool_entries(credentials: list[dict[str, str]], entry: dict[str, Any] | None = None, status: str = "all") -> list[dict[str, str]]:
    """Return safe, secret-free mailbox rows for the admin pool detail dialog."""
    requested = str(status or "all").strip().lower()
    aliases = {"retryable": {"failed"}, "invalid": {"login_required", "token_invalid"}, "all": None}
    with _OUTLOOK_STATE_LOCK:
        state_store = _read_outlook_state()
    rows: list[dict[str, str]] = []
    for item in expand_outlook_aliases(credentials, entry):
        email = str(item.get("email") or "").strip()
        state_item = state_store.get(email.lower()) or {}
        current = str(state_item.get("state") or "available")
        if current == "in_use" and _outlook_available(state_store, email):
            current = "available"
        wanted = aliases.get(requested, {requested})
        if wanted is not None and current not in wanted:
            continue
        rows.append({
            "email": email,
            "login_email": str(item.get("login_email") or item.get("alias_of") or email),
            "alias_of": str(item.get("alias_of") or ""),
            "state": current,
            "reason": str(state_item.get("reason") or "")[:240],
            "updated_at": str(state_item.get("updated_at") or ""),
        })
    return rows


def remove_outlook_invalid_credentials(credentials: list[dict[str, str]], entry: dict[str, Any] | None = None) -> tuple[list[dict[str, str]], int]:
    """Remove base credentials whose original address or aliases are permanently invalid."""
    with _OUTLOOK_STATE_LOCK:
        state_store = _read_outlook_state()
        invalid = {"login_required", "token_invalid"}
        kept: list[dict[str, str]] = []
        removed_pool_addresses: set[str] = set()
        removed = 0
        for credential in credentials:
            expanded = expand_outlook_aliases([credential], entry)
            if any(str((state_store.get(str(item.get("email") or "").lower()) or {}).get("state") or "") in invalid for item in expanded):
                removed += 1
                removed_pool_addresses.update(str(item.get("email") or "").lower() for item in expanded)
            else:
                kept.append(credential)
        for email in removed_pool_addresses:
            state_store.pop(email, None)
        if removed_pool_addresses:
            _write_outlook_state(state_store)
    return kept, removed


def prune_outlook_unused_credentials(credentials: list[dict[str, str]], entry: dict[str, Any] | None = None) -> tuple[list[dict[str, str]], int]:
    """Keep base credentials that have a recorded outcome; remove never-used ones."""
    with _OUTLOOK_STATE_LOCK:
        state = _read_outlook_state()
    recorded = {"in_use", "used", "failed", "login_required", "token_invalid"}
    kept: list[dict[str, str]] = []
    removed = 0
    for credential in credentials:
        expanded = expand_outlook_aliases([credential], entry)
        if any(str((state.get(str(item.get("email") or "").lower()) or {}).get("state") or "") in recorded for item in expanded):
            kept.append(credential)
        else:
            removed += 1
    return kept, removed


def reset_outlook_pool_state(scope: str = "all") -> int:
    scope = str(scope or "all").strip().lower()
    targets = {
        "retryable": {"in_use", "failed"},
        "invalid": {"login_required", "token_invalid"},
        "busy": {"in_use"},
        "used": {"used"},
    }.get(scope)
    with _OUTLOOK_STATE_LOCK:
        state = _read_outlook_state()
        if targets is None:
            removed = len(state)
            _write_outlook_state({})
            return removed
        remove = [key for key, value in state.items() if str(value.get("state") or "") in targets]
        for key in remove:
            state.pop(key, None)
        if remove:
            _write_outlook_state(state)
        return len(remove)


class OutlookTokenError(RuntimeError):
    """Microsoft OAuth failure with an explicit invalid-vs-transient classification."""

    def __init__(self, message: str, *, definitive: bool = True) -> None:
        super().__init__(message)
        self.definitive = definitive


class OutlookTokenProvider(_MailboxProvider):
    """Microsoft credential pool with Graph API, IMAP and Outlook plus-alias support."""

    name = "Microsoft ?????"
    poll_interval_seconds = 5.0

    def __init__(self, entry: dict[str, Any], proxy: str = "") -> None:
        self.name = str(entry.get("name") or self.name)
        self._entry = dict(entry)
        self._base_credentials = parse_outlook_credentials(str(entry.get("mailboxes") or ""))
        self._pool = expand_outlook_aliases(self._base_credentials, entry)
        if not self._pool:
            raise RuntimeError("Microsoft credential pool is empty or invalid")
        self._cursor = random.randrange(len(self._pool))
        self._session = _create_session(proxy)
        self._token_cache: dict[tuple[str, str], tuple[str, float]] = {}
        self.mode = str(entry.get("mode") or "auto").strip().lower()
        if self.mode not in {"graph", "imap", "auto"}:
            self.mode = "auto"
        self.imap_host = str(entry.get("imap_host") or OUTLOOK_DEFAULT_IMAP_HOST).strip() or OUTLOOK_DEFAULT_IMAP_HOST
        self.message_limit = _outlook_int(entry.get("message_limit"), 10, 1, 100)
        self.preflight_enabled = _outlook_bool(entry.get("preflight_enabled"), True)

    def preflight(self) -> dict[str, int]:
        """Validate each base Microsoft credential before a browser is started.

        Only definitive OAuth grant failures are marked token_invalid. Network,
        proxy and service errors remain retryable so a transient outage does not
        discard otherwise usable credentials.
        """
        result = {"checked": 0, "available": 0, "invalid": 0, "transient": 0}
        if not self.preflight_enabled:
            return result
        for credential in self._base_credentials:
            result["checked"] += 1
            context = json.dumps(credential, separators=(",", ":"))
            try:
                # list_messages follows the configured Graph/IMAP/auto mode and
                # proves both refresh-token usability and actual mailbox access.
                self.list_messages(credential["email"], context)
            except OutlookTokenError as exc:
                if not exc.definitive:
                    result["transient"] += 1
                    print(f"[mail] Microsoft mailbox preflight deferred: {credential['email']} ({type(exc).__name__}: {exc})", flush=True)
                    continue
                reason = str(exc)[:240]
                expanded = expand_outlook_aliases([credential], self._entry)
                with _OUTLOOK_STATE_LOCK:
                    state = _read_outlook_state()
                    for item in expanded:
                        email = str(item.get("email") or "").lower()
                        if email:
                            state[email] = {"state": "token_invalid", "reason": reason, "updated_at": datetime.now(UTC).isoformat()}
                    _write_outlook_state(state)
                result["invalid"] += 1
                print(f"[mail] Microsoft mailbox preflight invalid: {credential['email']} ({exc})", flush=True)
            except Exception as exc:
                result["transient"] += 1
                print(f"[mail] Microsoft mailbox preflight deferred: {credential['email']} ({type(exc).__name__}: {exc})", flush=True)
            else:
                result["available"] += 1
                # A successful re-check can recover an old invalid marker.
                expanded = expand_outlook_aliases([credential], self._entry)
                with _OUTLOOK_STATE_LOCK:
                    state = _read_outlook_state()
                    changed = False
                    for item in expanded:
                        email = str(item.get("email") or "").lower()
                        if str((state.get(email) or {}).get("state") or "") in {"token_invalid", "login_required"}:
                            state.pop(email, None)
                            changed = True
                    if changed:
                        _write_outlook_state(state)
        return result

    def create_mailbox(self) -> dict[str, str]:
        with _OUTLOOK_STATE_LOCK:
            state = _read_outlook_state()
            selected_index = next((index for index in ((self._cursor + offset) % len(self._pool) for offset in range(len(self._pool))) if _outlook_available(state, self._pool[index]["email"])), None)
            if selected_index is None:
                raise RuntimeError("Microsoft credential pool has no available mailbox")
            selected = self._pool[selected_index]
            self._cursor = (selected_index + 1) % len(self._pool)
            state[selected["email"].lower()] = {"state": "in_use", "reason": "", "updated_at": datetime.now(UTC).isoformat()}
            _write_outlook_state(state)
        return {"address": selected["email"], "token": json.dumps(selected, separators=(",", ":"))}

    @staticmethod
    def _credentials(token: str) -> dict[str, str]:
        try:
            value = json.loads(token)
        except json.JSONDecodeError as exc:
            raise OutlookTokenError("Microsoft credential context was lost") from exc
        if not isinstance(value, dict):
            raise OutlookTokenError("Microsoft credential context was lost")
        result = {key: str(value.get(key) or "").strip() for key in ("email", "client_id", "refresh_token", "login_email")}
        result["password"] = str(value.get("password") or "")
        if not result["email"] or not result["client_id"] or not result["refresh_token"]:
            raise OutlookTokenError("Microsoft credential is missing client_id or refresh_token")
        result["login_email"] = result["login_email"] or result["email"]
        return result

    def _access_token(self, credential: dict[str, str], scope: str = "", *, with_scope: bool = False) -> str:
        """Refresh an access token using the standalone worker's compatible order.

        The first attempt intentionally omits ``scope``.  Older consumer refresh
        tokens can be restricted to Outlook REST and reject a new Graph/IMAP scope.
        Callers that need a resource-specific token can retry with ``with_scope``.
        """
        cache_scope = scope if with_scope else "compat"
        key = (credential["email"].lower(), cache_scope)
        cached = self._token_cache.get(key)
        if cached and time.monotonic() < cached[1]:
            return cached[0]

        payload = {
            "client_id": credential["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": credential["refresh_token"],
        }
        if with_scope and scope:
            payload["scope"] = scope
        failures: list[tuple[str, bool]] = []
        for endpoint in OUTLOOK_TOKEN_URLS:
            endpoint_name = endpoint.rsplit("/", 4)[-4]
            try:
                response = self._session.post(
                    endpoint,
                    data=payload,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=30,
                    verify=False,
                )
            except Exception as exc:
                # Proxy, TLS and network errors must not poison a mailbox pool.
                failures.append((f"{endpoint_name}: {type(exc).__name__}", False))
                continue
            if response.status_code == 200:
                try:
                    body = response.json() or {}
                except Exception:
                    body = {}
                token = str(body.get("access_token") or "").strip()
                if token:
                    self._token_cache[key] = (token, time.monotonic() + 600)
                    return token
                failures.append((f"{endpoint_name}: no access_token", False))
                continue
            try:
                body = response.json() or {}
            except Exception:
                body = {}
            error = str(body.get("error") or "").strip().lower()
            description = str(body.get("error_description") or "").replace("\n", " ").strip()
            aadsts = re.search(r"AADSTS\d+", description, flags=re.IGNORECASE)
            detail = " ".join(part for part in (error, aadsts.group(0).upper() if aadsts else "") if part)
            definitive = response.status_code == 400 and error in {"invalid_grant", "invalid_client", "unauthorized_client"}
            failures.append((f"{endpoint_name}: HTTP {response.status_code}{f' ({detail})' if detail else ''}", definitive))
        message = "; ".join(item[0] for item in failures) or "no token endpoint succeeded"
        raise OutlookTokenError(f"Microsoft token refresh failed: {message}", definitive=bool(failures) and all(item[1] for item in failures))

    @staticmethod
    def _normalise_graph_messages(payload: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in payload.get("value", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            body = item.get("body") if isinstance(item.get("body"), dict) else {}
            result.append({
                "id": str(item.get("id") or ""),
                "subject": str(item.get("subject") or ""),
                "content": str(body.get("content") or item.get("bodyPreview") or ""),
            })
        return result

    def _graph_messages(self, credential: dict[str, str]) -> list[dict[str, Any]]:
        token_errors: list[OutlookTokenError] = []
        request_errors: list[str] = []
        # First mirror the legacy worker (no requested scope), then ask Graph for
        # an explicit Mail.Read token only if the original token cannot read Graph.
        for with_scope in (False, True):
            try:
                token = self._access_token(credential, OUTLOOK_GRAPH_SCOPE, with_scope=with_scope)
            except OutlookTokenError as exc:
                token_errors.append(exc)
                continue
            response = self._session.get(
                OUTLOOK_GRAPH_MESSAGES_URL,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"$top": self.message_limit, "$orderby": "receivedDateTime desc", "$select": "id,subject,body,bodyPreview"},
                timeout=30,
                verify=False,
            )
            if response.status_code == 200:
                return self._normalise_graph_messages(response.json() or {})
            request_errors.append(f"Microsoft Graph mailbox request failed: HTTP {response.status_code}")
        if request_errors:
            raise RuntimeError("; ".join(request_errors))
        raise token_errors[-1] if token_errors else OutlookTokenError("Microsoft token refresh failed")

    def _outlook_rest_messages(self, credential: dict[str, str]) -> list[dict[str, Any]]:
        """Fallback for older personal-account refresh tokens bound to Outlook REST."""
        token = self._access_token(credential)
        response = self._session.get(
            OUTLOOK_REST_MESSAGES_URL,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params={"$top": self.message_limit, "$orderby": "ReceivedDateTime desc", "$select": "Id,Subject,Body,BodyPreview"},
            timeout=30,
            verify=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Outlook REST mailbox request failed: HTTP {response.status_code}")
        result: list[dict[str, Any]] = []
        payload = response.json() or {}
        for item in payload.get("value", []) if isinstance(payload, dict) else []:
            if not isinstance(item, dict):
                continue
            body = item.get("Body") if isinstance(item.get("Body"), dict) else {}
            result.append({
                "id": str(item.get("Id") or ""),
                "subject": str(item.get("Subject") or ""),
                "content": str(body.get("Content") or item.get("BodyPreview") or ""),
            })
        return result

    @staticmethod
    def _decode_header(value: str) -> str:
        try:
            return str(make_header(decode_header(value)))
        except Exception:
            return value

    def _imap_messages(self, credential: dict[str, str]) -> list[dict[str, Any]]:
        token = self._access_token(credential, OUTLOOK_IMAP_SCOPE, with_scope=True)
        auth = f"user={credential['login_email']}\x01auth=Bearer {token}\x01\x01"
        client = imaplib.IMAP4_SSL(self.imap_host)
        try:
            client.authenticate("XOAUTH2", lambda _: auth.encode("utf-8"))
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError("Microsoft IMAP cannot select INBOX")
            status, data = client.uid("search", None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            result: list[dict[str, Any]] = []
            for uid in reversed(data[0].split()[-self.message_limit:]):
                status, fetched = client.uid("fetch", uid, "(RFC822)")
                if status != "OK":
                    continue
                raw = next((part[1] for part in fetched if isinstance(part, tuple) and isinstance(part[1], bytes)), b"")
                if not raw:
                    continue
                message = message_from_bytes(raw, policy=policy.default)
                content: list[str] = []
                for part in message.walk() if message.is_multipart() else [message]:
                    if part.get_content_maintype() == "multipart":
                        continue
                    try:
                        content.append(str(part.get_content() or ""))
                    except Exception:
                        continue
                result.append({"id": self._decode_header(str(message.get("Message-ID") or uid.decode("utf-8", "replace"))), "subject": self._decode_header(str(message.get("Subject") or "")), "content": "\n".join(content)})
            return result
        finally:
            try:
                client.logout()
            except Exception:
                pass

    def list_messages(self, address: str, provider_token: str = "") -> list[dict[str, Any]]:
        credential = self._credentials(provider_token)
        errors: list[Exception] = []
        if self.mode in {"graph", "auto"}:
            try:
                return self._graph_messages(credential)
            except Exception as exc:
                if self.mode == "graph":
                    raise
                errors.append(exc)
        if self.mode == "auto":
            try:
                return self._outlook_rest_messages(credential)
            except Exception as exc:
                errors.append(exc)
        if self.mode in {"imap", "auto"}:
            try:
                return self._imap_messages(credential)
            except Exception as exc:
                errors.append(exc)
        if errors and all(isinstance(exc, OutlookTokenError) for exc in errors):
            raise OutlookTokenError("; ".join(str(exc) for exc in errors))
        raise RuntimeError("; ".join(
            f"{type(exc).__name__}: {exc}" for exc in errors
        ) or "Microsoft mailbox read failed")

    def message_content(self, message_id: str, provider_token: str = "") -> str:
        return ""

    def mark_result(self, address: str, provider_token: str, success: bool, reason: str = "") -> None:
        reason = str(reason or "")[:240]
        try:
            credential = self._credentials(provider_token)
            login_email = credential.get("login_email") or address
        except Exception:
            login_email = address
        state_name = "used" if success else ("token_invalid" if "Microsoft token refresh failed" in reason else "failed")
        with _OUTLOOK_STATE_LOCK:
            state = _read_outlook_state()
            state[address.lower()] = {"state": state_name, "reason": reason, "updated_at": datetime.now(UTC).isoformat()}
            _write_outlook_state(state)

    def close(self) -> None:
        self._session.close()


class MailboxPool:
    def __init__(self, providers: list[dict[str, Any]], proxy: str = "") -> None:
        enabled = [p for p in providers if bool(p.get("enabled", True))]
        if not enabled:
            raise RuntimeError("At least one mailbox provider must be enabled")
        factories = {"gptmail": GptMailProvider, "tempmail_lol": TempMailLolProvider, "outlook_token": OutlookTokenProvider}
        self._providers: list[_MailboxProvider] = []
        unsupported: list[str] = []
        for entry in enabled:
            provider_type = str(entry.get("type") or "gptmail").strip().lower()
            factory = factories.get(provider_type)
            if factory is None:
                unsupported.append(provider_type)
                continue
            self._providers.append(factory(entry, proxy))
        if unsupported:
            raise RuntimeError(f"Unsupported mailbox provider types: {', '.join(unsupported)}")
        self._cursor = random.randrange(len(self._providers))

    def preflight(self) -> dict[str, int]:
        """Run optional provider self-tests before the browser registration flow."""
        total = {"checked": 0, "available": 0, "invalid": 0, "transient": 0}
        for provider in self._providers:
            checker = getattr(provider, "preflight", None)
            if not callable(checker):
                continue
            outcome = checker()
            for key in total:
                total[key] += int(outcome.get(key) or 0)
        return total

    def acquire(self) -> Mailbox:
        errors: list[str] = []
        for offset in range(len(self._providers)):
            index = (self._cursor + offset) % len(self._providers)
            provider = self._providers[index]
            try:
                created = provider.create_mailbox()
                if isinstance(created, dict):
                    address, provider_token = str(created.get("address") or ""), str(created.get("token") or "")
                else:
                    address, provider_token = str(created), ""
                if not address:
                    raise RuntimeError("Mailbox provider did not return an address")
                self._cursor = (index + 1) % len(self._providers)
                print(f"[mail] {provider.name} allocated mailbox: {address}", flush=True)
                return Mailbox(address=address, provider_index=index, provider_token=provider_token)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        raise RuntimeError("All mailbox providers failed to allocate an address; " + "; ".join(errors))

    @staticmethod
    def _inline_message_content(item: dict[str, Any]) -> str:
        values: list[str] = []
        for key in ("content", "text", "text_content", "body", "html", "html_content"):
            value = item.get(key)
            if isinstance(value, str) and value:
                values.append(value)
            elif isinstance(value, dict):
                values.extend(str(part) for part in value.values() if isinstance(part, str) and part)
        return "\n".join(values)

    @staticmethod
    def _sleep_until_next_poll(deadline: float, seconds: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(max(seconds, 0.0), remaining))
        return time.monotonic() < deadline

    def mark_result(self, mailbox: Mailbox, *, success: bool, reason: str = "") -> None:
        try:
            context = json.loads(mailbox.token())
            provider = self._providers[int(context.get("provider_index"))]
            marker = getattr(provider, "mark_result", None)
            if callable(marker):
                marker(mailbox.address, str(context.get("provider_token") or ""), success, reason)
        except Exception:
            pass

    @staticmethod
    def _message_id(item: dict[str, Any]) -> str:
        return str(item.get("id") or item.get("token") or item.get("message_id") or "")

    def capture_inbox_baseline(self, address: str, token: str) -> set[str]:
        """Snapshot inbox IDs *before* xAI sends an OTP.

        Microsoft plus aliases share one physical inbox.  Without this snapshot a
        verification email sent to an earlier alias can be mistaken for the OTP
        of the current registration attempt.
        """
        try:
            context = json.loads(token)
            provider = self._providers[int(context.get("provider_index"))]
            provider_token = str(context.get("provider_token") or "")
            messages = provider.list_messages(address, provider_token)
            known = {self._message_id(item) for item in messages if self._message_id(item)}
            print(f"[mail] inbox baseline captured: {len(known)} messages", flush=True)
            return known
        except OutlookTokenError:
            raise
        except Exception as exc:
            # The registration can still continue; a later inbox read will use an
            # empty baseline, but this condition is visible in the logs.
            print(f"[mail] inbox baseline capture failed: {type(exc).__name__}: {exc}", flush=True)
            return set()

    def wait_for_code(
        self,
        address: str,
        token: str,
        timeout: int = 120,
        known_message_ids: set[str] | None = None,
    ) -> str:
        try:
            context = json.loads(token)
            provider = self._providers[int(context.get("provider_index"))]
            provider_token = str(context.get("provider_token") or "")
        except Exception as exc:
            raise RuntimeError("Mailbox context was lost") from exc

        deadline = time.monotonic() + timeout
        poll_interval = max(3.0, float(getattr(provider, "poll_interval_seconds", 3.0)))
        old_ids: set[str] = set(known_message_ids or ())
        # The runner normally supplies a baseline captured before submitting the
        # email. For direct callers, build a safe baseline now and never accept a
        # code found in that historical snapshot.
        next_wait = 0.0 if known_message_ids is not None else poll_interval
        if known_message_ids is None:
            try:
                existing = provider.list_messages(address, provider_token)
                old_ids.update(self._message_id(item) for item in existing if self._message_id(item))
                print(f"[mail] inbox baseline captured: {len(old_ids)} messages", flush=True)
            except MailboxRateLimited as exc:
                next_wait = max(poll_interval, exc.retry_after or 15.0)
                print(f"[mail] {exc.provider_name} inbox rate limited; waiting {next_wait:.0f}s before retry", flush=True)
            except OutlookTokenError:
                raise
            except Exception as exc:
                print(f"[mail] initial inbox check failed: {type(exc).__name__}: {exc}", flush=True)

        print(f"[mail] waiting for {address} verification code, timeout {timeout}s", flush=True)
        while self._sleep_until_next_poll(deadline, next_wait):
            try:
                for item in provider.list_messages(address, provider_token):
                    message_id = self._message_id(item)
                    if not message_id or message_id in old_ids:
                        continue
                    inline = self._inline_message_content(item)
                    code = extract_verification_code(inline) or extract_verification_code(str(item.get("subject") or ""))
                    if not code:
                        # GptMail only returns the full body from its detail API;
                        # TempMail.lol normally has it inline, avoiding a second
                        # /inbox request for every polling iteration.
                        content = provider.message_content(message_id, provider_token)
                        code = extract_verification_code(content)
                    if code:
                        print(f"[mail] verification code received: {code}", flush=True)
                        return code
                    # The message is immutable and not a verification email;
                    # do not fetch its detail again on every later inbox poll.
                    old_ids.add(message_id)
                next_wait = poll_interval
            except MailboxRateLimited as exc:
                next_wait = max(poll_interval, exc.retry_after or 15.0)
                print(f"[mail] {exc.provider_name} inbox rate limited; waiting {next_wait:.0f}s before retry", flush=True)
            except OutlookTokenError:
                raise
            except Exception as exc:
                next_wait = poll_interval
                print(f"[mail] inbox polling failed: {type(exc).__name__}: {exc}", flush=True)
        raise VerificationCodeTimeout("Timed out waiting for a verification code")

    def close(self) -> None:
        for provider in self._providers:
            provider.close()


def extract_verification_code(content: str) -> str | None:
    # Mail bodies often contain CSS such as `text-size` inside <style> blocks.
    # Strip non-visible script/style content before looking for an ABC-123 code,
    # otherwise CSS identifiers can be submitted as a verification code.
    clean = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", content or "", flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean)
    contextual = re.search(r"(?:code|confirmation|validate|below)[:\s]+([A-Z0-9]{3,4}-[A-Z0-9]{3,4})", clean, re.I)
    if contextual:
        return contextual.group(1).upper()
    hyphenated = re.search(r"(?<![&#])\b([A-Z0-9]{3,4}-[A-Z0-9]{3,4})\b", clean, re.I)
    if hyphenated:
        return hyphenated.group(1).upper()
    numeric = re.search(r"(?<![#&])\b(\d{6})\b", clean)
    return numeric.group(1) if numeric else None


__all__ = ["Mailbox", "MailboxPool", "MailboxRateLimited", "OutlookTokenError", "OutlookTokenProvider", "TempMailLolProvider", "VerificationCodeTimeout", "expand_outlook_aliases", "extract_verification_code", "outlook_pool_entries", "outlook_pool_stats", "parse_outlook_credentials", "remove_outlook_invalid_credentials", "reset_outlook_pool_state"]
