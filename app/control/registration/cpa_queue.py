from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from .cpa import export_cookies_from_page, export_cpa_auth, record_cpa_task_result


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
        try:
            result = export_cpa_auth(account=account, cpa=self._cpa, cookies=cookies)
        except Exception:
            record_cpa_task_result(self._cpa, {"ok": False, "error": "export exception"})
            raise
        record_cpa_task_result(self._cpa, result)
        if result.get("ok"):
            print(f"[cpa] OIDC export completed: {result.get('path')}", flush=True)
            probe = result.get("probe_models") if isinstance(result.get("probe_models"), dict) else {}
            model_ids = [
                str(item)
                for item in (probe.get("model_ids") or result.get("model_ids") or [])
                if str(item)
            ]
            # A CPA file is the source of truth for the Build account. Model
            # probing is optional metadata and may be empty while /models is
            # unavailable or its response shape is changing. Do not discard
            # the account just because the probe returned no IDs.
            if bool(self._cpa.get("auto_import_build", True)) and result.get("path"):
                try:
                    from app.control.build import import_cpa_auth_file
                    imported = import_cpa_auth_file(str(result["path"]), model_ids=model_ids)
                    created_routes = 0
                    if model_ids:
                        from app.control.build.routes import store as build_routes
                        created_routes = build_routes.sync_discovered(model_ids)
                    if model_ids:
                        print(f"[Build] CPA OAuth auto-imported: email={account.get('email') or ''} models={','.join(model_ids)} result={imported} routes_added={created_routes}", flush=True)
                    else:
                        print(f"[Build] CPA OAuth imported without model metadata; pending model sync: email={account.get('email') or ''} result={imported}", flush=True)
                except Exception as exc:
                    print(f"[Build] CPA OAuth auto-import failed (export preserved): {type(exc).__name__}: {exc}", flush=True)
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
