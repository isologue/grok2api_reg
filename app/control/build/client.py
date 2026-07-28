"""Grok Build (CLI OAuth) upstream transport."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import orjson

from app.control.build.accounts import BuildAccount, refresh_account
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
from app.platform.errors import UpstreamError
from app.platform.logging.request_trace import (
    fail_upstream_trace,
    finish_upstream_trace,
    start_upstream_trace,
    trace_enabled,
    trace_max_chars,
)


def _is_response_completed_event(raw: str) -> bool:
    """Recognize semantic SSE completion even if the consumer closes early."""
    line = str(raw or "").strip()
    if not line.startswith("data:"):
        return False
    try:
        event = orjson.loads(line[5:].strip())
    except orjson.JSONDecodeError:
        return False
    return isinstance(event, dict) and str(event.get("type") or "") == "response.completed"


def _headers(account: BuildAccount, *, model: str = "") -> dict[str, str]:
    headers = {str(k): str(v) for k, v in account.headers.items()}
    conversation_id = secrets.token_hex(16)
    request_id = secrets.token_hex(16)
    headers.update(
        {
            "Authorization": f"Bearer {account.access_token}",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "x-grok-client-surface": "tui",
            "x-grok-client-name": headers.get("x-grok-client-identifier", "grok-shell"),
            "x-grok-agent-id": secrets.token_hex(16),
            "x-grok-session-id": str(uuid.uuid4()),
            "x-grok-conv-id": conversation_id,
            "x-grok-conversation-id": conversation_id,
            "x-grok-req-id": request_id,
            "x-grok-request-id": request_id,
            "x-grok-session-id-legacy": str(uuid.uuid4()),
            "traceparent": f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01",
        }
    )
    if model:
        headers["x-grok-model-override"] = model
    return headers


async def fetch_models(account: BuildAccount) -> list[str]:
    account = await refresh_account(account)
    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    kwargs = build_session_kwargs(lease=lease)
    endpoint = account.base_url.rstrip("/") + "/models"
    trace_id = start_upstream_trace(
        account_token=account.access_token,
        endpoint=endpoint,
        payload={"operation": "build_models"},
    )
    async with ResettableSession(**kwargs) as session:
        try:
            response = await session.get(endpoint, headers=_headers(account), timeout=30.0)
        except Exception as exc:
            error = UpstreamError(f"Grok Build models transport failed: {exc}", status=502)
            fail_upstream_trace(trace_id, account_token=account.access_token, endpoint=endpoint, error=error, status=error.status)
            raise error from exc
    body = response.content.decode("utf-8", "replace")
    if response.status_code != 200:
        error = UpstreamError("Grok Build model sync failed", status=response.status_code, body=body[:600])
        fail_upstream_trace(trace_id, account_token=account.access_token, endpoint=endpoint, error=error, status=response.status_code)
        raise error
    data = orjson.loads(response.content)
    models = sorted({str(item.get("id") or "").strip() for item in data.get("data") or [] if isinstance(item, dict) and str(item.get("id") or "").strip()})
    finish_upstream_trace(trace_id, account_token=account.access_token, endpoint=endpoint, response={"models": models}, completed=True)
    return models


async def create_response(account: BuildAccount, payload: dict[str, Any]) -> dict[str, Any]:
    account = await refresh_account(account)
    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    kwargs = build_session_kwargs(lease=lease)
    endpoint = account.base_url.rstrip("/") + "/responses"
    trace_id = start_upstream_trace(account_token=account.access_token, endpoint=endpoint, payload=payload)
    async with ResettableSession(**kwargs) as session:
        try:
            response = await session.post(endpoint, headers={**_headers(account, model=str(payload.get("model") or "")), "Content-Type": "application/json"}, data=orjson.dumps(payload), timeout=120.0)
        except Exception as exc:
            error = UpstreamError(f"Grok Build response transport failed: {exc}", status=502)
            fail_upstream_trace(trace_id, account_token=account.access_token, endpoint=endpoint, error=error, status=error.status)
            raise error from exc
    body = response.content.decode("utf-8", "replace")
    if response.status_code < 200 or response.status_code >= 300:
        error = UpstreamError("Grok Build response failed", status=response.status_code, body=body[:1200])
        fail_upstream_trace(trace_id, account_token=account.access_token, endpoint=endpoint, error=error, status=response.status_code)
        raise error
    data = orjson.loads(response.content)
    finish_upstream_trace(trace_id, account_token=account.access_token, endpoint=endpoint, response=data, completed=True)
    return data


async def stream_response(account: BuildAccount, payload: dict[str, Any]) -> AsyncGenerator[str, None]:
    account = await refresh_account(account)
    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    kwargs = build_session_kwargs(lease=lease)
    endpoint = account.base_url.rstrip("/") + "/responses"
    trace_id = start_upstream_trace(account_token=account.access_token, endpoint=endpoint, payload=payload)
    async with ResettableSession(**kwargs) as session:
        try:
            response = await session.post(endpoint, headers={**_headers(account, model=str(payload.get("model") or "")), "Content-Type": "application/json", "Accept": "text/event-stream"}, data=orjson.dumps(payload), timeout=120.0, stream=True)
        except Exception as exc:
            error = UpstreamError(f"Grok Build response transport failed: {exc}", status=502)
            fail_upstream_trace(trace_id, account_token=account.access_token, endpoint=endpoint, error=error, status=error.status)
            raise error from exc
        if response.status_code < 200 or response.status_code >= 300:
            body = response.content.decode("utf-8", "replace")[:1200]
            error = UpstreamError("Grok Build response failed", status=response.status_code, body=body)
            fail_upstream_trace(trace_id, account_token=account.access_token, endpoint=endpoint, error=error, status=response.status_code)
            raise error
        lines: list[str] = []
        chars = 0
        limit = trace_max_chars() if trace_id and trace_enabled() else 0
        complete = False
        semantic_complete = False
        failed = False
        try:
            async for line in response.aiter_lines():
                value = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                semantic_complete = semantic_complete or _is_response_completed_event(value)
                if limit and chars < limit:
                    remaining = limit - chars
                    lines.append(value[:remaining])
                    chars += min(len(value), remaining)
                yield value
            complete = True
        except Exception as exc:
            failed = True
            error = UpstreamError(f"Grok Build stream read failed: {exc}", status=502)
            fail_upstream_trace(trace_id, account_token=account.access_token, endpoint=endpoint, error=error, status=error.status)
            raise error from exc
        finally:
            if not failed:
                finish_upstream_trace(trace_id, account_token=account.access_token, endpoint=endpoint, response="\n".join(lines), completed=complete or semantic_complete)


__all__ = ["create_response", "fetch_models", "stream_response"]
