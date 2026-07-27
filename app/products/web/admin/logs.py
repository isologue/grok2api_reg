"""Admin log viewer endpoints for application and upstream trace logs."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.platform.paths import log_dir


router = APIRouter(prefix="/logs", tags=["Admin - Logs"])

_LOG_FILE_RE = re.compile(r"^app_\d{4}-\d{2}-\d{2}\.log$")
_MAX_READ_BYTES = 2 * 1024 * 1024


def _log_files() -> list[Path]:
    directory = log_dir()
    if not directory.is_dir():
        return []
    return sorted(
        (
            item
            for item in directory.iterdir()
            if item.is_file() and _LOG_FILE_RE.fullmatch(item.name)
        ),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )


def _safe_log_file(name: str) -> Path:
    if not _LOG_FILE_RE.fullmatch(name or ""):
        raise HTTPException(status_code=404, detail="Log file not found")
    candidate = log_dir() / name
    try:
        candidate.resolve().relative_to(log_dir().resolve())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Log file not found") from exc
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="Log file not found")
    return candidate


def _read_tail(path: Path) -> str:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > _MAX_READ_BYTES:
            handle.seek(-_MAX_READ_BYTES, 2)
            handle.readline()  # discard a partial first line
        data = handle.read()
    return data.decode("utf-8", "replace")


@router.get("/files")
async def list_log_files():
    files = []
    for item in _log_files():
        stat = item.stat()
        files.append(
            {
                "name": item.name,
                "size": stat.st_size,
                "modified_at": int(stat.st_mtime),
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            }
        )
    return {"files": files}


@router.get("/tail")
async def read_log_tail(
    file: str = Query(..., min_length=1),
    lines: int = Query(500, ge=20, le=2_000),
    query: str = Query("", max_length=300),
    trace_only: bool = Query(False),
):
    path = _safe_log_file(file)
    all_lines = _read_tail(path).splitlines()
    needle = query.strip().lower()
    if needle:
        all_lines = [line for line in all_lines if needle in line.lower()]
    if trace_only:
        all_lines = [line for line in all_lines if "TRACE_UPSTREAM" in line]
    selected = all_lines[-lines:]
    return {
        "file": path.name,
        "lines": selected,
        "matched": len(all_lines),
        "truncated_source": path.stat().st_size > _MAX_READ_BYTES,
    }


__all__ = ["router"]
