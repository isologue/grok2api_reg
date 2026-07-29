"""OpenAI Chat Completions compatibility for Grok Build Responses."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import orjson

from app.platform.errors import ValidationError
from .service import create as create_response
from .service import stream as stream_response


def _content_to_text(content: Any, *, param: str) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValidationError("message content must be text or text blocks", param=param)
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind in {"text", "input_text", "output_text"}:
            parts.append(str(item.get("text") or ""))
        elif kind:
            raise ValidationError(f"Grok Build chat does not support content type {kind!r}", param=param)
    return "\n".join(part for part in parts if part)


def messages_to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = str(message.get("role") or "user")
        text = _content_to_text(message.get("content"), param=f"messages[{index}].content")
        item: dict[str, Any] = {"role": role, "content": [{"type": "input_text", "text": text}]}
        if role == "tool" and message.get("tool_call_id"):
            item["tool_call_id"] = str(message["tool_call_id"])
        result.append(item)
    return result


def _tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    converted: list[dict[str, Any]] = []
    for item in tools:
        if not isinstance(item, dict) or item.get("type") != "function":
            raise ValidationError("Grok Build currently supports function tools only", param="tools")
        fn = item.get("function") or {}
        if not isinstance(fn, dict) or not fn.get("name"):
            raise ValidationError("function tool requires a name", param="tools")
        converted.append({
            "type": "function",
            "name": str(fn["name"]),
            "description": str(fn.get("description") or ""),
            "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return converted


def _text_and_calls(response: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    texts: list[str] = []
    calls: list[dict[str, Any]] = []
    for item in response.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call":
            calls.append({
                "id": str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {"name": str(item.get("name") or ""), "arguments": str(item.get("arguments") or "{}")},
            })
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                texts.append(str(content.get("text") or ""))
    return "".join(texts), calls


def _completion(response: dict[str, Any], model: str) -> dict[str, Any]:
    text, calls = _text_and_calls(response)
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    prompt = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    completion = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = calls
    return {
        "id": "chatcmpl_" + str(response.get("id") or uuid.uuid4().hex),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion},
    }


def _payload(*, model: str, messages: list[dict[str, Any]], temperature: float, top_p: float, tools: list[dict[str, Any]] | None, tool_choice: Any, max_tokens: int | None, stream: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "input": messages_to_input(messages), "stream": stream, "temperature": temperature, "top_p": top_p}
    build_tools = _tools(tools)
    if build_tools:
        payload["tools"] = build_tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if max_tokens:
        payload["max_output_tokens"] = max_tokens
    return payload


async def completions(*, model: str, messages: list[dict[str, Any]], stream: bool, temperature: float, top_p: float, tools: list[dict[str, Any]] | None = None, tool_choice: Any = None, max_tokens: int | None = None) -> dict[str, Any] | AsyncGenerator[str, None]:
    payload = _payload(model=model, messages=messages, temperature=temperature, top_p=top_p, tools=tools, tool_choice=tool_choice, max_tokens=max_tokens, stream=stream)
    if not stream:
        return _completion(await create_response(model=model, payload=payload, operation="chat.completions"), model)

    async def _run() -> AsyncGenerator[str, None]:
        sent_role = False
        async for raw in stream_response(model=model, payload=payload, operation="chat.completions"):
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            data_text = line[5:].strip()
            if data_text == "[DONE]":
                break
            try:
                event = orjson.loads(data_text)
            except orjson.JSONDecodeError:
                continue
            kind = str(event.get("type") or "")
            if kind == "response.output_text.delta":
                delta = str(event.get("delta") or "")
                if not delta:
                    continue
                payload_chunk: dict[str, Any] = {"id": "chatcmpl_build", "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": ({"role": "assistant", "content": delta} if not sent_role else {"content": delta}), "finish_reason": None}]}
                sent_role = True
                yield "data: " + orjson.dumps(payload_chunk).decode() + "\n\n"
            elif kind in {"response.completed", "response.incomplete", "response.failed"}:
                break
        finish = {"id": "chatcmpl_build", "object": "chat.completion.chunk", "created": int(time.time()), "model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield "data: " + orjson.dumps(finish).decode() + "\n\n"
        yield "data: [DONE]\n\n"
    return _run()


__all__ = ["completions", "messages_to_input"]
