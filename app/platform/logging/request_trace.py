"""Bounded, redacted upstream request tracing for the admin log viewer."""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import orjson

from app.platform.config.snapshot import get_config
from .logger import logger


_AUDIT_LOCK = threading.RLock()
_AUDIT_CONTEXT: dict[str, dict[str, Any]] = {}
_AUDIT_CONTEXT_LIMIT = 2_000

# Runtime-only token -> human-readable account identity map. It is populated
# by the control-plane synchronizer and never persisted in trace/audit data.
_AUDIT_ACCOUNT_LABELS: dict[str, str] = {}


_SENSITIVE_EXACT_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "password",
        "refresh_token",
        "access_token",
        "id_token",
        "token",
        "cookie",
        "session",
        "secret",
        "credential",
    }
)
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "password",
    "refresh_token",
    "access_token",
    "id_token",
    "api_key",
    "apikey",
    "cookie",
    "session",
    "secret",
    "credential",
)


def _is_sensitive_key(key: str) -> bool:
    """Mask credentials without hiding normal request counters such as tokens."""
    lowered = key.lower()
    return (
        lowered in _SENSITIVE_EXACT_KEYS
        or lowered.endswith("_token")
        or any(part in lowered for part in _SENSITIVE_KEY_PARTS)
    )


def trace_enabled() -> bool:
    """Return whether detailed upstream tracing is enabled in the live config."""
    return get_config().get_bool("logging.trace.enabled", True)


def trace_max_chars() -> int:
    try:
        value = int(get_config().get_int("logging.trace.max_chars", 16_000))
    except (TypeError, ValueError):
        value = 16_000
    return min(max(value, 1_024), 1_000_000)


def audit_enabled() -> bool:
    """Return whether the persistent request-audit journal is enabled."""
    return get_config().get_bool("logging.audit.enabled", True)


def replace_audit_account_labels(labels: Mapping[str, str]) -> None:
    """Atomically replace runtime-only audit account labels after a full sync."""
    cleaned = {
        str(token): str(label).strip()
        for token, label in labels.items()
        if str(token).strip() and str(label).strip()
    }
    with _AUDIT_LOCK:
        _AUDIT_ACCOUNT_LABELS.clear()
        _AUDIT_ACCOUNT_LABELS.update(cleaned)


def update_audit_account_label(token: str, label: str) -> None:
    """Update one runtime-only label after an incremental account change."""
    token = str(token or "").strip()
    label = str(label or "").strip()
    if not token:
        return
    with _AUDIT_LOCK:
        if label:
            _AUDIT_ACCOUNT_LABELS[token] = label
        else:
            _AUDIT_ACCOUNT_LABELS.pop(token, None)


def remove_audit_account_label(token: str) -> None:
    """Forget a deleted account's audit label."""
    token = str(token or "").strip()
    if token:
        with _AUDIT_LOCK:
            _AUDIT_ACCOUNT_LABELS.pop(token, None)


def _audit_account_label(account_token: str) -> str:
    """Return a safe display identity for an account, never the raw SSO."""
    token = str(account_token or "")
    with _AUDIT_LOCK:
        label = _AUDIT_ACCOUNT_LABELS.get(token)
    return label or _mask_account(token)


def _trace_audit_context(
    *,
    trace_id: str,
    account_token: str,
    endpoint: str,
    payload: Any,
    public_model_override: str = "",
    upstream_model_override: str = "",
) -> None:
    """Retain bounded metadata until the matching response/error trace arrives."""
    if not audit_enabled() or "cli-chat-proxy.grok.com" in endpoint:
        # Grok Build records richer audits in app.products.build.service.
        return
    parsed = urlparse(endpoint)
    operation = "upstream"
    public_model = ""
    upstream_model = ""
    streaming = False
    if isinstance(payload, Mapping):
        operation = str(payload.get("operation") or operation)
        nested = payload.get("payload")
        public_model = str(payload.get("model") or "")
        streaming = bool(payload.get("stream"))
        if isinstance(nested, Mapping):
            public_model = public_model or str(nested.get("model") or "")
            streaming = streaming or bool(nested.get("stream"))
    if "console.x.ai" in parsed.netloc:
        provider = "console"
        operation = "responses" if operation == "upstream" else operation
    elif "imagine" in operation or "image" in operation:
        provider = "image"
    elif "video" in operation:
        provider = "video"
    else:
        provider = "grok"
        if operation == "upstream" and "app-chat" in parsed.path:
            operation = "chat"
    public_model = str(public_model_override or public_model or "")
    upstream_model = str(upstream_model_override or upstream_model or public_model or "")
    context = {
        "provider": provider,
        "operation": operation,
        "public_model": public_model,
        "upstream_model": upstream_model,
        "account": _audit_account_label(account_token),
        "endpoint": endpoint,
        "request": payload,
        "streaming": streaming,
        "started": time.monotonic(),
    }
    with _AUDIT_LOCK:
        if len(_AUDIT_CONTEXT) >= _AUDIT_CONTEXT_LIMIT:
            _AUDIT_CONTEXT.pop(next(iter(_AUDIT_CONTEXT)), None)
        _AUDIT_CONTEXT[trace_id] = context


def _finish_trace_audit(trace_id: str | None, *, response: Any = None, error: BaseException | str | None = None, status: int | None = None) -> None:
    if not trace_id:
        return
    with _AUDIT_LOCK:
        context = _AUDIT_CONTEXT.pop(trace_id, None)
    if context is None:
        return
    try:
        from app.platform.request_audit import record
        duration_ms = int(max(0.0, time.monotonic() - float(context["started"])) * 1000)
        record(
            provider=str(context["provider"]),
            operation=str(context["operation"]),
            public_model=str(context["public_model"]),
            upstream_model=str(context["upstream_model"]),
            account=str(context["account"]),
            endpoint=str(context["endpoint"]),
            status_code=int(status if status is not None else (502 if error else 200)),
            streaming=bool(context.get("streaming")),
            duration_ms=duration_ms,
            request=context["request"],
            response=response,
            error=str(error or ""),
        )
    except Exception as exc:  # Audit failures must never break the actual request.
        logger.debug("request audit write skipped: {}", exc)


def _mask_account(token: str) -> str:
    text = str(token or "")
    if len(text) <= 12:
        return "***"
    return f"{text[:8]}...{text[-4:]}"


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth >= 8:
        return "[max-depth]"
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            lowered = key.lower()
            if _is_sensitive_key(lowered):
                sanitized[key] = "***"
            else:
                sanitized[key] = _redact(raw_value, depth=depth + 1)
        return sanitized
    if isinstance(value, (list, tuple, set)):
        return [_redact(item, depth=depth + 1) for item in value]
    if isinstance(value, bytes):
        return f"[bytes:{len(value)}]"
    if isinstance(value, str):
        if value.startswith("data:"):
            return f"[data-url:{len(value)}]"
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _bounded_json(value: Any) -> str:
    raw = orjson.dumps(_redact(value)).decode("utf-8", "replace")
    limit = trace_max_chars()
    if len(raw) <= limit:
        return raw
    return raw[:limit] + f"...[truncated {len(raw) - limit} chars]"


def _bounded_text(value: Any) -> str:
    text = str(value or "")
    limit = trace_max_chars()
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def start_upstream_trace(
    *,
    account_token: str,
    endpoint: str,
    payload: Any,
    public_model: str = "",
    upstream_model: str = "",
) -> str | None:
    """Write a redacted request trace and begin a persistent audit item."""
    if not trace_enabled() and not audit_enabled():
        return None
    trace_id = secrets.token_hex(6)
    _trace_audit_context(
        trace_id=trace_id,
        account_token=account_token,
        endpoint=endpoint,
        payload=payload,
        public_model_override=public_model,
        upstream_model_override=upstream_model,
    )
    if trace_enabled():
        logger.info(
            "TRACE_UPSTREAM {}",
            _bounded_json(
                {
                    "event": "request",
                    "trace_id": trace_id,
                    "account": _mask_account(account_token),
                    "endpoint": endpoint,
                    "payload": payload,
                }
            ),
        )
    return trace_id


def finish_upstream_trace(
    trace_id: str | None,
    *,
    account_token: str,
    endpoint: str,
    response: Any,
    completed: bool,
) -> None:
    """Write bounded response content and finish the matching audit item."""
    if not trace_id:
        return
    _finish_trace_audit(trace_id, response=response, status=200)
    if not trace_enabled():
        return
    logger.info(
        "TRACE_UPSTREAM {}",
        _bounded_json(
            {
                "event": "response",
                "trace_id": trace_id,
                "account": _mask_account(account_token),
                "endpoint": endpoint,
                "completed": completed,
                "content": _bounded_text(response),
            }
        ),
    )


def fail_upstream_trace(
    trace_id: str | None,
    *,
    account_token: str,
    endpoint: str,
    error: BaseException | str,
    status: int | None = None,
) -> None:
    """Write bounded failure content and finish the matching audit item."""
    if not trace_id:
        return
    _finish_trace_audit(trace_id, error=error, status=status)
    if not trace_enabled():
        return
    details = getattr(error, "details", None)
    logger.warning(
        "TRACE_UPSTREAM {}",
        _bounded_json(
            {
                "event": "error",
                "trace_id": trace_id,
                "account": _mask_account(account_token),
                "endpoint": endpoint,
                "status": status,
                "error": str(error),
                "details": details,
            }
        ),
    )


__all__ = [
    "trace_enabled",
    "audit_enabled",
    "trace_max_chars",
    "start_upstream_trace",
    "finish_upstream_trace",
    "fail_upstream_trace",
    "replace_audit_account_labels",
    "update_audit_account_label",
    "remove_audit_account_label",
]
