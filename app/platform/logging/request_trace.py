"""Bounded, redacted upstream request tracing for the admin log viewer."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

import orjson

from app.platform.config.snapshot import get_config
from .logger import logger


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
) -> str | None:
    """Write a redacted request record and return its correlation ID."""
    if not trace_enabled():
        return None
    trace_id = secrets.token_hex(6)
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
    """Write bounded response content for a previously traced upstream request."""
    if not trace_id:
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
    """Write a bounded failure record without exposing credentials.

    ``UpstreamError`` stores a short upstream response excerpt in ``details``.
    Keep that excerpt in the trace as well: it is usually the decisive clue for
    a 4xx/5xx response, while the normal redaction and length limits still
    apply.
    """
    if not trace_id:
        return

    details = getattr(error, "details", None)
    upstream_body = ""
    if isinstance(details, Mapping):
        upstream_body = details.get("body", "")

    logger.warning(
        "TRACE_UPSTREAM {}",
        _bounded_json(
            {
                "event": "error",
                "trace_id": trace_id,
                "account": _mask_account(account_token),
                "endpoint": endpoint,
                "status": status,
                "error": _bounded_text(error),
                "upstream_body": _bounded_text(upstream_body),
            }
        ),
    )


__all__ = [
    "trace_enabled",
    "trace_max_chars",
    "start_upstream_trace",
    "finish_upstream_trace",
    "fail_upstream_trace",
]
