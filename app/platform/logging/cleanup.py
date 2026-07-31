"""Scheduled cleanup for persistent audit and CPA task log artifacts."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from app.platform.logging.logger import logger
from app.platform.paths import log_path
from app.platform.request_audit import purge_before
from app.platform.runtime.clock import now_ms
from app.control.account.cleanup import seconds_until_next_daily_run

_DAY_MS = 86_400_000
_DAY_SECONDS = 86_400
_TASK_LOG_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.log\Z")


@dataclass(frozen=True)
class LogCleanupResult:
    """Counts produced by one persistent-log cleanup pass."""

    request_audits: int = 0
    cpa_task_logs: int = 0


def _cutoff_ms(retention_days: int, *, current_ms: int | None = None) -> int:
    """Return the oldest timestamp still retained by a positive policy."""
    return (now_ms() if current_ms is None else int(current_ms)) - max(0, int(retention_days)) * _DAY_MS


def purge_cpa_task_logs_once(
    retention_days: int,
    *,
    current_time: float | None = None,
    active_task_ids: set[str] | None = None,
) -> int:
    """Remove old registration/CPA task log files.

    ``0`` disables this cleanup.  Only direct ``*.log`` children of the
    registration log directory are considered; CPA Auth JSON and task manifests
    are intentionally never removed by this policy.
    """
    days = int(retention_days)
    if days <= 0:
        return 0

    cutoff = (time.time() if current_time is None else float(current_time)) - days * _DAY_SECONDS
    directory = log_path("registration")
    if not directory.is_dir():
        return 0

    active_ids = {str(value).strip() for value in (active_task_ids or set()) if str(value).strip()}
    removed = 0
    for path in directory.glob("*.log"):
        try:
            if (
                not _TASK_LOG_NAME.fullmatch(path.name)
                or not path.is_file()
                or path.stem in active_ids
                or path.stat().st_mtime >= cutoff
            ):
                continue
            path.unlink()
            removed += 1
        except (FileNotFoundError, OSError):
            # A task may finish or rotate while cleanup is scanning.  Continue
            # with the remaining files rather than failing the daily job.
            continue
    return removed


def purge_logs_once(
    *,
    request_audit_retention_days: int,
    cpa_task_retention_days: int,
    active_cpa_task_ids: set[str] | None = None,
) -> LogCleanupResult:
    """Run one pass using the two independently configurable policies."""
    audit_days = int(request_audit_retention_days)
    cpa_days = int(cpa_task_retention_days)
    audit_count = (
        purge_before(_cutoff_ms(audit_days))
        if audit_days > 0
        else 0
    )
    cpa_count = (
        purge_cpa_task_logs_once(
            cpa_days,
            active_task_ids=active_cpa_task_ids,
        )
        if cpa_days > 0
        else 0
    )
    return LogCleanupResult(request_audits=audit_count, cpa_task_logs=cpa_count)


async def run_daily_log_cleanup(
    get_settings: Callable[[], dict[str, Any]],
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run persistent-log cleanup once per configured daily maintenance time.

    Settings are read both before sleeping and immediately before cleanup, so
    changing retention values in the admin page takes effect without a restart
    (at the next scheduled pass).  A value of ``0`` disables that policy.
    """
    while True:
        settings = get_settings()
        delay = seconds_until_next_daily_run(
            now_ms_value=now_ms(),
            run_at=str(settings.get("run_at") or "03:30"),
        )
        try:
            if stop_event is None:
                await asyncio.sleep(delay)
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    return
                except asyncio.TimeoutError:
                    pass

            settings = get_settings()
            result = purge_logs_once(
                request_audit_retention_days=max(0, int(settings.get("request_audit_retention_days", 7))),
                cpa_task_retention_days=max(0, int(settings.get("cpa_task_retention_days", 7))),
                active_cpa_task_ids={
                    str(value)
                    for value in (settings.get("active_cpa_task_ids") or set())
                    if str(value).strip()
                },
            )
            if result.request_audits or result.cpa_task_logs:
                logger.info(
                    "persistent log cleanup completed: request_audits={} cpa_task_logs={}",
                    result.request_audits,
                    result.cpa_task_logs,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "persistent log cleanup failed: error_type={} error={}",
                type(exc).__name__,
                exc,
            )


__all__ = [
    "LogCleanupResult",
    "purge_cpa_task_logs_once",
    "purge_logs_once",
    "run_daily_log_cleanup",
]
