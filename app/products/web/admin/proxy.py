"""Admin endpoint for validating a configured outbound proxy."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/proxy", tags=["Admin - Proxy"])
_TEST_URL = "https://grok.com/"
_ALLOWED_SCHEMES = {"http", "https", "socks4", "socks5"}


class ProxyTestRequest(BaseModel):
    proxy: str = Field(min_length=1, max_length=2048)


def _normalize_proxy(value: str) -> str:
    proxy = str(value or "").strip()
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    parsed = urlsplit(proxy)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError("代理协议仅支持 http、https、socks4 和 socks5")
    if not parsed.hostname or not parsed.port:
        raise ValueError("代理地址必须包含主机和端口")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def _safe_proxy_label(proxy: str) -> str:
    try:
        parsed = urlsplit(proxy)
        host = parsed.hostname or "未知主机"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"
    except Exception:
        return "已配置代理"


def _safe_error(exc: BaseException) -> str:
    text = str(exc).replace("\n", " ").strip()
    # Do not let a proxy password leak into the response or server log.
    text = re.sub(r"(?i)(https?|socks[45]?)://[^\s/@]+:[^\s/@]+@", r"\1://***:***@", text)
    return text[:500] or type(exc).__name__


def _probe(proxy: str) -> dict[str, Any]:
    started = time.perf_counter()
    label = _safe_proxy_label(proxy)
    try:
        try:
            from curl_cffi import requests as curl_requests

            response = curl_requests.get(
                _TEST_URL,
                proxy=proxy,
                impersonate="chrome136",
                timeout=12,
                allow_redirects=False,
            )
        except ImportError:
            import requests

            response = requests.get(
                _TEST_URL,
                proxies={"http": proxy, "https": proxy},
                timeout=12,
                allow_redirects=False,
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        status = int(response.status_code)
        if status == 407:
            return {
                "ok": False,
                "connected": False,
                "status_code": status,
                "elapsed_ms": elapsed_ms,
                "message": "代理服务器要求认证（HTTP 407），请检查账号密码",
                "proxy": label,
                "target": _TEST_URL,
            }
        return {
            "ok": True,
            "connected": True,
            "status_code": status,
            "elapsed_ms": elapsed_ms,
            "message": f"代理已连通，上游返回 HTTP {status}",
            "proxy": label,
            "target": _TEST_URL,
        }
    except Exception as exc:
        return {
            "ok": False,
            "connected": False,
            "status_code": None,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "message": f"代理连接失败：{_safe_error(exc)}",
            "proxy": label,
            "target": _TEST_URL,
        }


@router.post("/test")
async def test_proxy(req: ProxyTestRequest):
    try:
        proxy = _normalize_proxy(req.proxy)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # curl_cffi performs blocking I/O; keep the event loop responsive while a
    # proxy is being tested, especially when the endpoint is unreachable.
    return await asyncio.to_thread(_probe, proxy)


__all__ = ["router"]
