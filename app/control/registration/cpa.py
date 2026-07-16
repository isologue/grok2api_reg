from __future__ import annotations

import contextlib
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from app.platform.paths import data_path

LogFn = Callable[[str], None]


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def export_cookies_from_page(page: Any) -> list[dict[str, Any]]:
    """Snapshot browser cookies while the successful registration page is alive."""
    if page is None:
        return []
    for getter in (
        lambda: page.cookies(all_domains=True, all_info=True),
        lambda: page.cookies(all_domains=True),
        lambda: page.cookies(),
    ):
        try:
            cookies = getter()
            if isinstance(cookies, list):
                return [item for item in cookies if isinstance(item, dict)]
        except TypeError:
            continue
        except Exception:
            continue
    return []


def _output_dir(raw: str) -> Path:
    value = (raw or "").strip()
    if not value:
        return data_path("cpa_auths")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = data_path(str(path))
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def export_cpa_auth(
    *,
    account: dict[str, Any],
    cpa: dict[str, Any],
    cookies: list[dict[str, Any]] | None = None,
    log: LogFn | None = None,
) -> dict[str, Any]:
    """Protocol-first SSO -> OIDC mint and CLIProxyAPI-compatible file export."""
    log = log or (lambda message: print(message, flush=True))
    if not _bool(cpa.get("enabled"), False):
        return {"ok": False, "skipped": True, "reason": "disabled"}

    email = str(account.get("email") or "").strip()
    password = str(account.get("password") or "")
    sso = str(account.get("sso") or "").strip()
    if not email or not sso:
        return {"ok": False, "error": "missing email or sso"}

    from .cpa_xai import mint_and_export

    auth_dir = _output_dir(str(cpa.get("auth_dir") or ""))
    proxy = str(cpa.get("proxy") or "").strip()
    base_url = str(cpa.get("base_url") or "https://cli-chat-proxy.grok.com/v1").strip()
    timeout = float(cpa.get("timeout_sec") or 300)
    prefer_protocol = _bool(cpa.get("prefer_protocol"), True)
    protocol_only = _bool(cpa.get("protocol_only"), False)

    log(
        "[cpa] start OIDC mint "
        f"email={email} output={auth_dir} proxy={'configured' if proxy else 'none'} "
        f"protocol={'only' if protocol_only else ('preferred' if prefer_protocol else 'disabled')}"
    )
    result = mint_and_export(
        email=email,
        password=password,
        auth_dir=auth_dir,
        page=None,
        proxy=proxy or None,
        headless=_bool(cpa.get("headless"), False),
        base_url=base_url,
        probe=_bool(cpa.get("probe_after_write"), True),
        probe_chat=_bool(cpa.get("probe_chat"), False),
        browser_timeout_sec=timeout,
        force_standalone=True,
        cookies=cookies or None,
        sso=sso,
        reuse_browser=False,
        recycle_every=max(1, int(cpa.get("browser_recycle_every") or 15)),
        prefer_protocol=prefer_protocol,
        protocol_only=protocol_only,
        protocol_poll_timeout_sec=max(30, int(cpa.get("protocol_poll_timeout_sec") or 90)),
        log=lambda message: log(f"[cpa] {message}"),
    )

    if (
        not result.get("ok")
        and result.get("path")
        and str(result.get("error") or "").startswith("token ok but grok-4.5 not listed")
        and not _bool(cpa.get("probe_required"), False)
    ):
        result["ok"] = True
        result["probe_warning"] = result.pop("error")
        log(f"[cpa] probe warning ignored: {result['probe_warning']}")

    if result.get("ok") and result.get("path") and _bool(cpa.get("copy_to_hotload"), False):
        hotload = str(cpa.get("hotload_dir") or "").strip()
        if hotload:
            target = Path(hotload).expanduser()
            if not target.is_absolute():
                target = data_path(str(target))
            target.mkdir(parents=True, exist_ok=True)
            destination = target / Path(str(result["path"])).name
            shutil.copy2(str(result["path"]), destination)
            with contextlib.suppress(OSError):
                os.chmod(destination, 0o600)
            result["hotload_path"] = str(destination)
            log(f"[cpa] copied to hotload: {destination}")

    if not result.get("ok"):
        failure = auth_dir / "cpa_auth_failed.txt"
        with failure.open("a", encoding="utf-8") as handle:
            handle.write(f"{email}----{result.get('error') or 'unknown'}----{int(time.time())}\n")
        if _bool(cpa.get("mint_required"), False):
            raise RuntimeError(f"CPA mint required but failed: {result.get('error') or 'unknown'}")
    return result
