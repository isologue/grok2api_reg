"""Persistent public-model to Grok Build upstream-model routing."""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel, Field

from app.platform.errors import ValidationError
from app.platform.paths import data_path

_ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}$")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_name(value: str, field: str) -> str:
    result = str(value or "").strip()
    if not _ROUTE_RE.fullmatch(result):
        raise ValidationError(f"Invalid {field}; use 1-255 letters, digits, '.', '_', ':', '/', or '-'")
    return result


class BuildModelRoute(BaseModel):
    public_id: str
    upstream_model: str
    enabled: bool = True
    origin: str = "discovered"
    created_at: int = Field(default_factory=_now_ms)
    updated_at: int = Field(default_factory=_now_ms)


class BuildModelRouteStore:
    def __init__(self) -> None:
        self._path = data_path("build_model_routes.json")
        self._items: dict[str, BuildModelRoute] = {}
        self._loaded = False
        self._lock = threading.RLock()

    def _load_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            values = raw.get("routes") if isinstance(raw, dict) else raw
            if not isinstance(values, list):
                return
            for value in values:
                if isinstance(value, dict):
                    with contextlib.suppress(Exception):
                        item = BuildModelRoute.model_validate(value)
                        self._items[item.public_id.lower()] = item
        except FileNotFoundError:
            return

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "routes": [item.model_dump() for item in self._items.values()]}
        temp = self._path.with_suffix(".tmp")
        temp.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        with contextlib.suppress(OSError):
            os.chmod(temp, 0o600)
        temp.replace(self._path)
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    def list(self) -> list[dict[str, Any]]:
        from .accounts import store as accounts
        # Seed routes for accounts imported before model-route support existed.
        self.sync_discovered(accounts.known_models())
        with self._lock:
            self._load_locked()
            rows = []
            for item in sorted(self._items.values(), key=lambda value: value.public_id.lower()):
                value = item.model_dump()
                value["available"] = accounts.has_model(item.upstream_model)
                rows.append(value)
            return rows

    def get(self, public_id: str) -> BuildModelRoute | None:
        with self._lock:
            self._load_locked()
            item = self._items.get(str(public_id or "").strip().lower())
            return item.model_copy(deep=True) if item else None

    def upstream_for(self, public_id: str) -> str | None:
        item = self.get(public_id)
        return item.upstream_model if item and item.enabled else None

    def sync_discovered(self, models: list[str]) -> int:
        created = 0
        with self._lock:
            self._load_locked()
            for raw in models:
                model = str(raw or "").strip()
                if not model or not _ROUTE_RE.fullmatch(model):
                    continue
                key = model.lower()
                if key not in self._items:
                    self._items[key] = BuildModelRoute(public_id=model, upstream_model=model, origin="discovered")
                    created += 1
            if created:
                self._save_locked()
        return created

    def upsert(self, *, public_id: str, upstream_model: str, enabled: bool = True) -> BuildModelRoute:
        public_id = _validate_name(public_id, "public model ID")
        upstream_model = _validate_name(upstream_model, "upstream model")
        with self._lock:
            self._load_locked()
            existing = self._items.get(public_id.lower())
            item = BuildModelRoute(
                public_id=public_id,
                upstream_model=upstream_model,
                enabled=bool(enabled),
                origin="manual",
                created_at=existing.created_at if existing else _now_ms(),
                updated_at=_now_ms(),
            )
            self._items[public_id.lower()] = item
            self._save_locked()
            return item.model_copy(deep=True)

    def delete(self, public_id: str) -> bool:
        with self._lock:
            self._load_locked()
            deleted = self._items.pop(str(public_id or "").strip().lower(), None) is not None
            if deleted:
                self._save_locked()
            return deleted


store = BuildModelRouteStore()

__all__ = ["BuildModelRoute", "BuildModelRouteStore", "store"]
