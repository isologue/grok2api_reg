from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .cpa import export_cookies_from_page, export_cpa_auth


class CpaExportQueue:
    """Single-file OIDC exporter, keeping device-flow mints spaced apart."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._cpa = dict(config.get("cpa") or {})
        if not str(self._cpa.get("proxy") or "").strip():
            self._cpa["proxy"] = str(config.get("browser_proxy") or config.get("proxy") or "").strip()
        self._enabled = bool(self._cpa.get("enabled", False))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cpa-export") if self._enabled else None
        self._futures: list[Future[dict[str, Any]]] = []
        self._last_started = 0.0
        self._lock = threading.Lock()

    def submit(self, account: dict[str, Any], page: Any) -> None:
        if not self._executor:
            return
        snapshot = {
            "email": str(account.get("email") or ""),
            "password": str(account.get("password") or ""),
            "sso": str(account.get("sso") or ""),
        }
        cookies = export_cookies_from_page(page)
        self._futures.append(self._executor.submit(self._run, snapshot, cookies))
        print(f"[cpa] queued OIDC export for {snapshot['email']}", flush=True)

    def _run(self, account: dict[str, Any], cookies: list[dict[str, Any]]) -> dict[str, Any]:
        try:
            gap = max(0.0, float(self._cpa.get("mint_gap_sec") or 25))
        except (TypeError, ValueError):
            gap = 25.0
        with self._lock:
            wait = gap - (time.monotonic() - self._last_started)
            if wait > 0:
                print(f"[cpa] mint gap protection: waiting {wait:.1f}s", flush=True)
                time.sleep(wait)
            self._last_started = time.monotonic()
        result = export_cpa_auth(account=account, cpa=self._cpa, cookies=cookies)
        if result.get("ok"):
            print(f"[cpa] OIDC export completed: {result.get('path')}", flush=True)
        elif not result.get("skipped"):
            print(f"[cpa] OIDC export failed: {result.get('error') or result}", flush=True)
        return result

    def drain(self) -> None:
        if not self._executor:
            return
        try:
            default_timeout = max(float(self._cpa.get("timeout_sec") or 300) + 120.0, 600.0)
            timeout = max(60.0, float(self._cpa.get("drain_timeout_sec") or default_timeout))
        except (TypeError, ValueError):
            timeout = 600.0
        for future in self._futures:
            try:
                # Each queued export gets its own wait budget.  A single global
                # 300-second deadline could close a still-working browser fallback
                # immediately before it receives the device-flow token.
                future.result(timeout=timeout)
            except TimeoutError:
                print(
                    f"[cpa] export wait timeout after {timeout:.0f}s; keeping the registration worker alive is required for a retry",
                    flush=True,
                )
            except Exception as exc:
                print(f"[cpa] background export exception: {type(exc).__name__}: {exc}", flush=True)
        self._executor.shutdown(wait=False, cancel_futures=True)
