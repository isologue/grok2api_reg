"""Small persistent request-audit journal for provider calls."""
from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import orjson

from app.platform.paths import data_path
from app.platform.logging.request_trace import _redact

_MAX_ITEMS = 10_000
_LOCK = threading.RLock()
_PATH = data_path("request_audits.jsonl")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _trim(value: Any, limit: int = 16_000) -> Any:
    data = _redact(value)
    try:
        text = orjson.dumps(data).decode("utf-8", "replace")
    except Exception:
        text = str(data)
    if len(text) <= limit:
        return data
    return {"truncated": True, "preview": text[:limit]}


def record(*, provider: str, operation: str, public_model: str, upstream_model: str, account: str, endpoint: str, status_code: int, streaming: bool, duration_ms: int, request: Any, response: Any = None, error: str = "") -> dict[str, Any]:
    item = {
        "id": uuid.uuid4().hex,
        "created_at": _now_ms(),
        "provider": provider,
        "operation": operation,
        "public_model": public_model,
        "upstream_model": upstream_model,
        "account": account,
        "endpoint": endpoint,
        "status_code": int(status_code or 0),
        "streaming": bool(streaming),
        "duration_ms": max(0, int(duration_ms or 0)),
        "request": _trim(request),
        "response": _trim(response) if response is not None else None,
        "error": str(error or "")[:2_000],
    }
    line = orjson.dumps(item).decode() + "\n"
    with _LOCK:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PATH.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line)
        _compact_locked()
    return item


def _compact_locked() -> None:
    with contextlib.suppress(FileNotFoundError, OSError):
        if _PATH.stat().st_size <= 32 * 1024 * 1024:
            return
        rows = _read_locked(limit=_MAX_ITEMS)
        temp = _PATH.with_suffix(".tmp")
        temp.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in rows))
        temp.replace(_PATH)


def _read_locked(*, limit: int = _MAX_ITEMS) -> list[dict[str, Any]]:
    if not _PATH.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with _PATH.open("rb") as handle:
        for raw in handle:
            with contextlib.suppress(orjson.JSONDecodeError):
                value = orjson.loads(raw)
                if isinstance(value, dict):
                    rows.append(value)
    return rows[-limit:]


def _filtered_items(*, query: str = "", provider: str = "", status: str = "") -> list[dict[str, Any]]:
    needle = str(query or "").strip().lower()
    with _LOCK:
        rows = list(reversed(_read_locked()))
    result: list[dict[str, Any]] = []
    for row in rows:
        if provider and row.get("provider") != provider:
            continue
        code = int(row.get("status_code") or 0)
        if status:
            normalized = str(status).strip().lower()
            if normalized.endswith("xx") and normalized[:1].isdigit():
                if code // 100 != int(normalized[0]):
                    continue
            elif normalized == "success":
                if not 200 <= code < 400:
                    continue
            elif normalized == "failed":
                if code < 400:
                    continue
            elif str(code) != normalized:
                continue
        if needle and needle not in " ".join(str(row.get(key) or "") for key in ("public_model", "upstream_model", "account", "operation", "error")).lower():
            continue
        result.append(row)
    return result


def list_items(*, limit: int = 200, query: str = "", provider: str = "", status: str = "") -> list[dict[str, Any]]:
    """Compatibility helper returning the newest matching audit rows."""
    safe_limit = min(max(int(limit or 200), 1), 1_000)
    return _filtered_items(query=query, provider=provider, status=status)[:safe_limit]


def list_page(*, page: int = 1, page_size: int = 50, query: str = "", provider: str = "", status: str = "") -> dict[str, Any]:
    """Return one newest-first audit page plus pagination metadata."""
    safe_page = max(1, int(page or 1))
    safe_size = min(max(int(page_size or 50), 10), 2_000)
    rows = _filtered_items(query=query, provider=provider, status=status)
    total = len(rows)
    total_pages = max(1, (total + safe_size - 1) // safe_size)
    current_page = min(safe_page, total_pages)
    start = (current_page - 1) * safe_size
    return {
        "items": rows[start:start + safe_size],
        "total": total,
        "page": current_page,
        "page_size": safe_size,
        "total_pages": total_pages,
    }


def get_item(item_id: str) -> dict[str, Any] | None:
    with _LOCK:
        for row in reversed(_read_locked()):
            if row.get("id") == item_id:
                return row
    return None


def summary() -> dict[str, Any]:
    with _LOCK:
        rows = _read_locked()
    total = len(rows)
    success = sum(1 for row in rows if 200 <= int(row.get("status_code") or 0) < 400)
    failed = sum(1 for row in rows if int(row.get("status_code") or 0) >= 400)
    return {"total": total, "success": success, "failed": failed, "latest_at": rows[-1].get("created_at") if rows else 0}

__all__ = ["record", "list_items", "list_page", "get_item", "summary"]
