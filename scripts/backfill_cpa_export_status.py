"""Backfill compact CPA export outcomes from historical failure marker files.

This maintenance task intentionally reads only the marker format written by the
CPA exporter: ``email----error----unix_timestamp``.  It never prints or writes
passwords, SSO values, access tokens, or refresh tokens.

Run inside the application container:
    python /app/scripts/backfill_cpa_export_status.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow direct execution as /app/scripts/backfill_cpa_export_status.py.
APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from app.control.account.backends.factory import create_repository
from app.control.account.commands import AccountPatch, ListAccountsQuery
from app.control.registration.archive import decrypt_profile
from app.platform.paths import data_path


_FAILURE_NAME = "cpa_auth_failed.txt"


def _parse_failure(path: Path) -> dict[str, Any] | None:
    """Return safe historical failure metadata, or None for malformed files."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    parts = raw.rsplit("----", 2)
    if len(parts) != 3:
        return None
    email, message, timestamp_raw = (part.strip() for part in parts)
    if not email or not message:
        return None
    try:
        timestamp = float(timestamp_raw)
        updated_at = datetime.fromtimestamp(timestamp, UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    folded = message.casefold()
    status = (
        "xai_denied"
        if "invalid_grant" in folded and "access denied" in folded
        else "retryable_failure"
    )
    return {
        "email": email.casefold(),
        "status": status,
        "updated_at": updated_at,
        "task_id": path.parent.name if path.parent.name else "legacy_cpa_failure",
        "message": message[:500],
        "timestamp": timestamp,
    }


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or not value.strip():
        return 0.0
    try:
        normalized = value.strip().replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


async def _all_records(repo: Any) -> list[Any]:
    records: list[Any] = []
    page = 1
    while True:
        result = await repo.list_accounts(
            ListAccountsQuery(page=page, page_size=2_000, include_deleted=False)
        )
        records.extend(result.items)
        if page >= result.total_pages:
            return records
        page += 1


async def main() -> int:
    root = data_path("cpa_auths")
    files = sorted(root.rglob(_FAILURE_NAME)) if root.is_dir() else []
    latest_by_email: dict[str, dict[str, Any]] = {}
    malformed = 0
    for path in files:
        item = _parse_failure(path)
        if item is None:
            malformed += 1
            continue
        current = latest_by_email.get(item["email"])
        if current is None or float(item["timestamp"]) > float(current["timestamp"]):
            latest_by_email[item["email"]] = item

    repo = create_repository()
    await repo.initialize()
    records = await _all_records(repo)
    patches: list[AccountPatch] = []
    matched = skipped_existing_success = skipped_newer = 0
    by_status = {"xai_denied": 0, "retryable_failure": 0}

    for record in records:
        profile = decrypt_profile(record.ext)
        email = str((profile or {}).get("email") or "").strip().casefold()
        item = latest_by_email.get(email)
        if item is None:
            continue
        matched += 1
        existing = record.ext.get("cpa_export") if isinstance(record.ext, dict) else None
        existing = existing if isinstance(existing, dict) else {}
        if str(existing.get("status") or "") == "success":
            skipped_existing_success += 1
            continue
        if _timestamp(existing.get("updated_at")) >= float(item["timestamp"]):
            skipped_newer += 1
            continue
        payload = {
            "status": item["status"],
            "updated_at": item["updated_at"],
            "task_id": item["task_id"],
            "message": item["message"],
        }
        patches.append(AccountPatch(token=record.token, ext_merge={"cpa_export": payload}))
        by_status[item["status"]] += 1

    if patches:
        result = await repo.patch_accounts(patches)
        patched = result.patched
    else:
        patched = 0
    await repo.close()

    print(
        "CPA export status backfill complete: "
        f"files={len(files)}, malformed={malformed}, distinct_failures={len(latest_by_email)}, "
        f"matched_accounts={matched}, patched={patched}, "
        f"xai_denied={by_status['xai_denied']}, retryable_failure={by_status['retryable_failure']}, "
        f"skipped_success={skipped_existing_success}, skipped_newer={skipped_newer}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
