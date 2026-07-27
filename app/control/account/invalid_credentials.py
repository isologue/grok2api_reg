"""Shared handling for upstream invalid-credential failures."""

from typing import TYPE_CHECKING

from app.platform.errors import UpstreamError
from app.platform.logging.logger import logger
from app.platform.runtime.clock import now_ms

from .commands import AccountPatch
from .enums import AccountStatus, FeedbackKind

if TYPE_CHECKING:
    from .repository import AccountRepository


async def mark_account_invalid_credentials(
    repo: "AccountRepository",
    token: str,
    exc: BaseException,
    *,
    source: str,
) -> bool:
    """Persist an account as expired for a real 401 or known invalid-token marker."""
    from app.dataplane.reverse.protocol.xai_usage import is_invalid_credentials_error

    status = getattr(exc, "status", None)
    invalid_marker = is_invalid_credentials_error(exc)
    if status != 401 and not invalid_marker:
        return False

    record = next(iter(await repo.get_accounts([token])), None)
    reason = "invalid_credentials" if invalid_marker else "unauthorized"
    ts = now_ms()
    ext = record.ext if record is not None else {}

    await repo.patch_accounts(
        [
            AccountPatch(
                token=token,
                status=AccountStatus.EXPIRED,
                last_fail_at=ts,
                last_fail_reason=reason,
                state_reason=reason,
                ext_merge={
                    **ext,
                    "expired_at": ts,
                    "expired_reason": reason,
                },
            )
        ]
    )
    logger.info(
        "account expired from {}: token={}... reason={} upstream_status={}",
        source,
        token[:10],
        reason,
        status,
    )
    return True


def is_transport_error(exc: BaseException | None) -> bool:
    """Return whether an error points to proxy/network transport, not the account."""
    if not isinstance(exc, UpstreamError):
        return False
    text = " ".join(
        [
            str(exc),
            str(exc.details.get("body", "") or ""),
        ]
    ).lower()
    markers = (
        "transport error",
        "transport failed",
        "connection error",
        "connection reset",
        "connect timeout",
        "read timeout",
        "timed out",
        "network is unreachable",
        "proxy error",
        "tls error",
        "ssl error",
    )
    return any(marker in text for marker in markers)


def feedback_kind_for_error(exc: BaseException | None) -> FeedbackKind:
    """Map an upstream exception to the appropriate account feedback kind."""
    if exc is None:
        return FeedbackKind.SERVER_ERROR
    # Check for known blocked/invalid body markers first — these override
    # the generic status-code mapping so that e.g. a 403 with "blocked-user"
    # body is treated as an account-level credential failure, not a generic
    # FORBIDDEN that only lowers health.
    from app.dataplane.reverse.protocol.xai_usage import is_invalid_credentials_error

    if is_invalid_credentials_error(exc):
        return FeedbackKind.UNAUTHORIZED
    if is_transport_error(exc):
        return FeedbackKind.TRANSPORT_ERROR
    status = getattr(exc, "status", 0)
    if status == 429:
        return FeedbackKind.RATE_LIMITED
    if status == 401:
        return FeedbackKind.UNAUTHORIZED
    if status == 403:
        return FeedbackKind.FORBIDDEN
    return FeedbackKind.SERVER_ERROR


__all__ = [
    "mark_account_invalid_credentials",
    "is_transport_error",
    "feedback_kind_for_error",
]
