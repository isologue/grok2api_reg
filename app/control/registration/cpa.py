from __future__ import annotations

import contextlib
import json
import os
import re
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
        path = data_path("cpa_auths")
    else:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = data_path(str(path))
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def cpa_auth_root(cpa: dict[str, Any]) -> Path:
    """Return the configured CPA root directory (without a task subdirectory)."""
    return _output_dir(str(cpa.get("auth_dir") or ""))


def _safe_task_id(value: Any) -> str:
    task_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", task_id):
        raise ValueError("invalid CPA task id")
    return task_id


def cpa_task_dir(cpa: dict[str, Any], task_id: Any) -> Path:
    """Resolve a task-private CPA output directory below the configured root."""
    directory = cpa_auth_root(cpa) / _safe_task_id(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _manifest_path(cpa: dict[str, Any], task_id: Any) -> Path:
    """Keep task metadata outside CPA auth directories so only auth JSON is exposed there."""
    directory = data_path("registration", "cpa_auth_tasks")
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_safe_task_id(task_id)}.json"


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_manifest(path: Path, value: dict[str, Any]) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(temp, 0o600)
    temp.replace(path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def initialize_cpa_task(
    cpa: dict[str, Any],
    task_id: str,
    *,
    started_at: str,
    requested_count: int,
) -> None:
    """Create task metadata without putting account credentials in it."""
    if not _bool(cpa.get("enabled"), False):
        return
    path = _manifest_path(cpa, task_id)
    manifest = _read_manifest(path)
    manifest.update({
        "task_id": _safe_task_id(task_id),
        "started_at": started_at,
        "finished_at": None,
        "state": "running",
        "requested_count": max(0, int(requested_count)),
        "exported_count": int(manifest.get("exported_count") or 0),
        "failed_count": int(manifest.get("failed_count") or 0),
    })
    _write_manifest(path, manifest)


def record_cpa_task_result(cpa: dict[str, Any], result: dict[str, Any]) -> None:
    """Update only aggregate per-task CPA export counters; no auth data is indexed."""
    task_id = str(cpa.get("task_id") or "").strip()
    if not task_id or not _bool(cpa.get("enabled"), False):
        return
    path = _manifest_path(cpa, task_id)
    manifest = _read_manifest(path)
    if not manifest:
        initialize_cpa_task(cpa, task_id, started_at="", requested_count=0)
        manifest = _read_manifest(path)
    if result.get("ok") and result.get("path"):
        manifest["exported_count"] = int(manifest.get("exported_count") or 0) + 1
    elif not result.get("skipped"):
        manifest["failed_count"] = int(manifest.get("failed_count") or 0) + 1
    _write_manifest(path, manifest)


def finalize_cpa_task(cpa: dict[str, Any], task_id: str, *, state: str, finished_at: str) -> None:
    if not _bool(cpa.get("enabled"), False):
        return
    path = _manifest_path(cpa, task_id)
    manifest = _read_manifest(path)
    if not manifest:
        return
    manifest["state"] = str(state or "completed")
    manifest["finished_at"] = finished_at
    _write_manifest(path, manifest)


def list_cpa_auth_tasks(cpa: dict[str, Any]) -> list[dict[str, Any]]:
    """List task-isolated CPA outputs. Legacy flat files remain exportable as one group."""
    root = cpa_auth_root(cpa)
    rows: list[dict[str, Any]] = []
    legacy_files = sorted(path for path in root.glob("*.json") if path.is_file())
    if legacy_files:
        rows.append({
            "task_id": "legacy",
            "state": "legacy",
            "started_at": "",
            "finished_at": "",
            "requested_count": 0,
            "exported_count": len(legacy_files),
            "failed_count": 0,
            "auth_count": len(legacy_files),
        })
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        try:
            task_id = _safe_task_id(directory.name)
        except ValueError:
            continue
        manifest = _read_manifest(_manifest_path(cpa, task_id))
        files = sorted(path for path in directory.glob("*.json") if path.is_file())
        rows.append({
            "task_id": task_id,
            "state": str(manifest.get("state") or "completed"),
            "started_at": str(manifest.get("started_at") or ""),
            "finished_at": str(manifest.get("finished_at") or ""),
            "requested_count": int(manifest.get("requested_count") or 0),
            "exported_count": int(manifest.get("exported_count") or len(files)),
            "failed_count": int(manifest.get("failed_count") or 0),
            "auth_count": len(files),
        })
    return sorted(rows, key=lambda item: (item["started_at"], item["task_id"]), reverse=True)


def cpa_auth_task_files(cpa: dict[str, Any], task_id: str) -> list[Path]:
    root = cpa_auth_root(cpa)
    if task_id == "legacy":
        return sorted(path for path in root.glob("*.json") if path.is_file())
    directory = root / _safe_task_id(task_id)
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


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

    task_id = str(cpa.get("task_id") or "").strip()
    auth_dir = cpa_task_dir(cpa, task_id) if task_id else cpa_auth_root(cpa)
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
