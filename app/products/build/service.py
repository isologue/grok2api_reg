"""Grok Build OAuth request execution, model routing, and request auditing."""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any

import orjson

from app.control.build import store
from app.control.build.client import create_response, stream_response
from app.control.build.routes import store as route_store
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError, ValidationError
from app.platform.logging.logger import logger
from app.platform.request_audit import record as record_audit


def _retry_count() -> int:
    return max(0, min(3, get_config().get_int("build.max_retries", 1)))


def _retryable(exc: BaseException) -> bool:
    return getattr(exc, "status", None) in {401, 403, 408, 409, 425, 429, 500, 502, 503, 504}


def _route(public_model: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    route = route_store.get(public_model)
    upstream = route.upstream_model if route and route.enabled else public_model
    if route is None and public_model != "grok-4.5":
        raise ValidationError(f"Build model route {public_model!r} was not found", param="model")
    body = dict(payload)
    body["model"] = upstream
    return upstream, body


def _account_label(lease: Any) -> str:
    return str(lease.account.email or lease.account.user_id or lease.account.id)


def _sse_response_state(raw: str) -> str:
    """Return xAI Responses API terminal state from one SSE data line."""
    line = str(raw or "").strip()
    if not line.startswith("data:"):
        return ""
    try:
        event = orjson.loads(line[5:].strip())
    except orjson.JSONDecodeError:
        return ""
    return str(event.get("type") or "") if isinstance(event, dict) else ""


async def create(*, model: str, payload: dict[str, Any], operation: str = "responses") -> dict[str, Any]:
    """Run a routed non-streaming Build request with account failover and audit."""
    upstream_model, body = _route(model, payload)
    excluded: set[str] = set()
    last_error: BaseException | None = None
    for attempt in range(_retry_count() + 1):
        lease = store.reserve(upstream_model, exclude_ids=excluded)
        started = time.monotonic()
        try:
            result = await create_response(lease.account, body)
        except BaseException as exc:
            last_error = exc
            store.release(lease, success=False, error=exc)
            record_audit(provider="build", operation=operation, public_model=model, upstream_model=upstream_model, account=_account_label(lease), endpoint=lease.account.base_url.rstrip("/")+"/responses", status_code=int(getattr(exc, "status", 502) or 502), streaming=False, duration_ms=int((time.monotonic()-started)*1000), request=body, error=str(exc))
            excluded.add(lease.account.id)
            if _retryable(exc) and attempt < _retry_count():
                logger.warning("Grok Build request retry: model={} upstream={} attempt={}/{} status={}", model, upstream_model, attempt + 1, _retry_count() + 1, getattr(exc, "status", None))
                continue
            raise
        else:
            store.release(lease, success=True)
            record_audit(provider="build", operation=operation, public_model=model, upstream_model=upstream_model, account=_account_label(lease), endpoint=lease.account.base_url.rstrip("/")+"/responses", status_code=200, streaming=False, duration_ms=int((time.monotonic()-started)*1000), request=body, response=result)
            return result
    if last_error:
        raise last_error
    raise UpstreamError("No available Grok Build account", status=429)


async def stream(*, model: str, payload: dict[str, Any], operation: str = "responses") -> AsyncGenerator[str, None]:
    """Run a routed Build SSE request and write an audit outcome on close."""
    upstream_model, body = _route(model, payload)
    excluded: set[str] = set()
    last_error: BaseException | None = None
    for attempt in range(_retry_count() + 1):
        lease = store.reserve(upstream_model, exclude_ids=excluded)
        started = time.monotonic()
        emitted = False
        success = False
        captured: list[str] = []
        try:
            async for line in stream_response(lease.account, body):
                emitted = True
                state = _sse_response_state(line)
                if state == "response.completed":
                    # The OpenAI/Anthropic adapters end their own output stream at
                    # this event. Mark the account successful before yielding it,
                    # otherwise generator close is incorrectly recorded as 502.
                    success = True
                elif state in {"response.incomplete", "response.failed"}:
                    raise UpstreamError(f"Grok Build stream ended with {state}", status=502)
                if len(captured) < 500:
                    captured.append(line)
                yield line + "\n"
            success = True
            return
        except BaseException as exc:
            # A downstream adapter normally closes immediately after receiving
            # response.completed. That is a successful terminal SSE event, not a
            # failed upstream request.
            if success and isinstance(exc, (GeneratorExit, asyncio.CancelledError)):
                return
            last_error = exc
            excluded.add(lease.account.id)
            if emitted or not _retryable(exc) or attempt >= _retry_count():
                raise
            logger.warning("Grok Build stream retry before first event: model={} upstream={} attempt={}/{} status={}", model, upstream_model, attempt + 1, _retry_count() + 1, getattr(exc, "status", None))
        finally:
            store.release(lease, success=success, error=None if success else last_error)
            record_audit(provider="build", operation=operation, public_model=model, upstream_model=upstream_model, account=_account_label(lease), endpoint=lease.account.base_url.rstrip("/")+"/responses", status_code=200 if success else int(getattr(last_error, "status", 502) or 502), streaming=True, duration_ms=int((time.monotonic()-started)*1000), request=body, response="\n".join(captured) if success else None, error="" if success else str(last_error or "stream failed"))
    if last_error:
        raise last_error
    raise UpstreamError("No available Grok Build account", status=429)


__all__ = ["create", "stream"]
