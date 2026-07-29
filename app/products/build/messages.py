"""Anthropic Messages compatibility for Grok Build Responses."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import orjson

from .chat import messages_to_input
from .service import create as create_response
from .service import stream as stream_response


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(str(item.get("text") or "") for item in system if isinstance(item, dict) and item.get("type") == "text")
    return ""


def _payload(*, model: str, messages: list[dict[str, Any]], system: Any, temperature: float, top_p: float, max_tokens: int | None, stream: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "input": messages_to_input(messages), "stream": stream, "temperature": temperature, "top_p": top_p}
    instructions = _system_text(system)
    if instructions:
        payload["instructions"] = instructions
    if max_tokens:
        payload["max_output_tokens"] = max_tokens
    return payload


def _text(response: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in response.get("output") or []:
        if isinstance(item, dict):
            for content in item.get("content") or []:
                if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                    parts.append(str(content.get("text") or ""))
    return "".join(parts)


async def create(*, model: str, messages: list[dict[str, Any]], system: Any, stream: bool, temperature: float, top_p: float, max_tokens: int | None = None) -> dict[str, Any] | AsyncGenerator[str, None]:
    payload = _payload(model=model, messages=messages, system=system, temperature=temperature, top_p=top_p, max_tokens=max_tokens, stream=stream)
    msg_id = "msg_" + uuid.uuid4().hex
    if not stream:
        response = await create_response(model=model, payload=payload, operation="messages")
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        return {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [{"type": "text", "text": _text(response)}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": int(usage.get("output_tokens") or 0)}}

    async def _run() -> AsyncGenerator[str, None]:
        now = int(time.time())
        yield _event("message_start", {"type": "message_start", "message": {"id": msg_id, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
        yield _event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        async for raw in stream_response(model=model, payload=payload, operation="messages"):
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            try:
                event = orjson.loads(line[5:].strip())
            except orjson.JSONDecodeError:
                continue
            if event.get("type") == "response.output_text.delta" and event.get("delta"):
                yield _event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": str(event["delta"])}})
            elif event.get("type") in {"response.completed", "response.incomplete", "response.failed"}:
                break
        yield _event("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _event("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}})
        yield _event("message_stop", {"type": "message_stop"})
    return _run()


def _event(name: str, value: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {orjson.dumps(value).decode()}\n\n"


__all__ = ["create"]
