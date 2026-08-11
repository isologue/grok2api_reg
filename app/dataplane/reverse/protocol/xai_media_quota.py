"""Grok Imagine media quota protocol.

The web client exposes a separate quota endpoint for free video credits.  This
module intentionally uses the project proxy runtime instead of a direct client.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import orjson

from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms

_ENDPOINT = "https://grok.com/rest/media/imagine/quota_info"
_DEFAULT_WINDOW_SECONDS = 86_400


def _parse_next_available_at(value: object, *, fallback_ms: int) -> int:
    text = str(value or "").strip()
    if not text:
        return fallback_ms
    # Python datetime accepts at most six fractional digits; HAR responses can
    # contain nanoseconds (for example .978651440Z).
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return fallback_ms


def parse_video_quota(body: dict, *, synced_at: int | None = None):
    """Convert ``video720p`` from quota_info to a ``QuotaWindow``."""
    from app.control.account.enums import QuotaSource
    from app.control.account.models import QuotaWindow

    synced = int(synced_at or now_ms())
    item = body.get("video720p") if isinstance(body, dict) else None
    if not isinstance(item, dict):
        return None
    try:
        remaining = max(0, int(item.get("remainingQueries") or 0))
    except (TypeError, ValueError):
        remaining = 0
    try:
        window_seconds = max(1, int(item.get("windowSizeSeconds") or _DEFAULT_WINDOW_SECONDS))
    except (TypeError, ValueError):
        window_seconds = _DEFAULT_WINDOW_SECONDS
    total_raw = item.get("totalQueries")
    try:
        total = max(remaining, int(total_raw)) if total_raw is not None else max(1, remaining)
    except (TypeError, ValueError):
        total = max(1, remaining)
    fallback_reset = synced + window_seconds * 1000
    reset_at = _parse_next_available_at(item.get("nextAvailableAt"), fallback_ms=fallback_reset)
    # available=false is authoritative even when a stale remainingQueries value
    # is returned by an upstream cache.
    if item.get("available") is False:
        remaining = 0
    return QuotaWindow(
        remaining=remaining,
        total=total,
        window_seconds=window_seconds,
        reset_at=reset_at,
        synced_at=synced,
        source=QuotaSource.REAL,
    )


def _feedback_kind_for_error(exc: BaseException, status: int | None):
    from app.control.proxy.models import ProxyFeedbackKind
    if status == 429:
        return ProxyFeedbackKind.RATE_LIMITED
    if status == 403:
        return ProxyFeedbackKind.CHALLENGE
    if status == 401:
        return ProxyFeedbackKind.UNAUTHORIZED
    if status and status >= 500:
        return ProxyFeedbackKind.UPSTREAM_5XX
    return ProxyFeedbackKind.TRANSPORT_ERROR


def _looks_like_invalid_token(exc: BaseException) -> bool:
    body = str(getattr(exc, "details", {}).get("body", "") or "").lower()
    return any(marker in body for marker in (
        "invalid-credentials", "invalid_grant", "session not found",
        "token expired", "token revoked", "blocked-user",
    ))


async def fetch_video_quota(token: str):
    """Fetch the account's independent free-video quota through the configured proxy."""
    from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind
    from app.dataplane.proxy import get_proxy_runtime
    from app.dataplane.reverse.transport.http import post_json

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    try:
        body = await post_json(
            _ENDPOINT,
            token,
            orjson.dumps({}),
            lease=lease,
            timeout_s=20.0,
        )
        await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200))
    except Exception as exc:
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        try:
            await proxy.feedback(lease, ProxyFeedback(kind=_feedback_kind_for_error(exc, status), status_code=status))
        except Exception:
            pass
        logger.debug("视频额度查询失败：token={}... status={} error={}", token[:10], status, exc)
        if _looks_like_invalid_token(exc):
            raise
        return None
    window = parse_video_quota(body)
    if window is None:
        logger.debug("视频额度响应缺少 video720p：token={}... body={}", token[:10], body)
    else:
        logger.debug("视频额度已同步：token={}... remaining={} reset_at={}", token[:10], window.remaining, window.reset_at)
    return window


__all__ = ["fetch_video_quota", "parse_video_quota"]
