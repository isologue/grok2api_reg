from __future__ import annotations

from typing import Any


def extract_model_ids(body: Any) -> list[str]:
    """Extract model identifiers from supported API response shapes."""
    found: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            found.append(value)

    def visit(value: Any, *, model_container: bool = False) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item, model_container=True)
            return
        if not isinstance(value, dict):
            if model_container:
                add(value)
            return

        for key in ("id", "model"):
            add(value.get(key))
        if model_container or any(key in value for key in ("owned_by", "created", "object", "type")):
            add(value.get("name"))

        for key in ("data", "models"):
            if key in value:
                visit(value.get(key), model_container=True)
        result = value.get("result")
        if isinstance(result, (dict, list)):
            visit(result, model_container=True)

    visit(body)
    return found
