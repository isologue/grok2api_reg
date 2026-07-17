"""Persistent settings and supervised child-process runtime for account registration."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import re
import sys
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.platform.paths import data_path, log_path
from .cpa import finalize_cpa_task, initialize_cpa_task

_DEFAULT_SETTINGS: dict[str, Any] = {
    "run": {
        "count": 1,
        "mailbox_attempts": 5,
        "code_timeout_sec": 120,
    },
    "proxy": "",
    "browser_proxy": "",
    "account": {"pool": "basic", "tags": ["registered"]},
    "mail": {"providers": []},
    "cpa": {
        "enabled": False,
        "auth_dir": "",
        "copy_to_hotload": False,
        "hotload_dir": "",
        "proxy": "",
        "base_url": "https://cli-chat-proxy.grok.com/v1",
        "prefer_protocol": True,
        "protocol_only": False,
        "protocol_poll_timeout_sec": 90,
        "timeout_sec": 300,
        "mint_gap_sec": 25,
        "drain_timeout_sec": 600,
        "probe_after_write": True,
        "probe_chat": False,
        "probe_required": False,
        "mint_required": False,
    },
}

def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", value)[:64] or "provider"


class RegistrationManager:
    """One supervised browser registration task per API process.

    Settings are intentionally stored below DATA_DIR rather than in the general
    config endpoint: mailbox secrets are excluded from generic configuration
    reads and never returned unmasked by this API.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._process: asyncio.subprocess.Process | None = None
        self._watch_task: asyncio.Task | None = None
        self._lines: deque[str] = deque(maxlen=300)
        self._runtime: dict[str, Any] = {
            "state": "idle",
            "task_id": "",
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "message": "尚未启动注册任务",
            "log_file": "",
        }

    @property
    def _dir(self) -> Path:
        path = data_path("registration")
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def _settings_path(self) -> Path:
        return self._dir / "settings.json"

    def _read_settings_raw(self) -> dict[str, Any]:
        if not self._settings_path.is_file():
            return copy.deepcopy(_DEFAULT_SETTINGS)
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
            return _deep_merge(_DEFAULT_SETTINGS, data if isinstance(data, dict) else {})
        except (OSError, json.JSONDecodeError):
            return copy.deepcopy(_DEFAULT_SETTINGS)

    def get_settings(self) -> dict[str, Any]:
        data = self._read_settings_raw()
        providers = []
        for item in (data.get("mail") or {}).get("providers") or []:
            if not isinstance(item, dict):
                continue
            clone = dict(item)
            secret = str(clone.pop("api_key", "") or "")
            clone["api_key"] = ""
            clone["api_key_configured"] = bool(secret)
            if str(clone.get("type") or "") == "outlook_token":
                mailbox_text = str(clone.pop("mailboxes", "") or "")
                from .mail import expand_outlook_aliases, outlook_pool_stats, parse_outlook_credentials
                credentials = parse_outlook_credentials(mailbox_text)
                expanded = expand_outlook_aliases(credentials, clone)
                clone["mailboxes"] = ""
                clone["mailboxes_configured"] = bool(mailbox_text)
                clone["mailboxes_base_count"] = len(credentials)
                clone["mailboxes_count"] = len(expanded)
                clone["mailboxes_alias_count"] = max(0, len(expanded) - len(credentials))
                clone["mailboxes_parse_stats"] = {"saved": len(credentials), "pending": 0}
                clone["mailboxes_stats"] = outlook_pool_stats(credentials, clone)
            providers.append(clone)
        data.setdefault("mail", {})["providers"] = providers
        cpa = dict(data.get("cpa") or {})
        cpa_proxy = str(cpa.pop("proxy", "") or "")
        cpa["proxy"] = ""
        cpa["proxy_configured"] = bool(cpa_proxy)
        data["cpa"] = cpa
        return data

    def save_settings(self, raw: dict[str, Any]) -> dict[str, Any]:
        existing = self._read_settings_raw()
        incoming = _deep_merge(_DEFAULT_SETTINGS, raw)
        mail = incoming.setdefault("mail", {})
        providers = mail.get("providers") or []
        if not isinstance(providers, list):
            raise ValueError("mail.providers 必须是数组")
        old_by_id = {
            str(item.get("id") or ""): item
            for item in (existing.get("mail") or {}).get("providers") or []
            if isinstance(item, dict)
        }
        normalized: list[dict[str, Any]] = []
        for index, value in enumerate(providers):
            if not isinstance(value, dict):
                raise ValueError(f"第 {index + 1} 个邮箱服务格式无效")
            item = dict(value)
            item["id"] = _safe_name(str(item.get("id") or uuid.uuid4().hex[:10]))
            item["type"] = str(item.get("type") or "gptmail").strip().lower()
            default_name = {"tempmail_lol": "TempMail.lol", "outlook_token": "Microsoft ?????"}.get(item["type"], "GptMail")
            item["name"] = str(item.get("name") or f"{default_name} {index + 1}").strip()[:80]
            item["enabled"] = bool(item.get("enabled", True))
            # All mailbox APIs share the configured mailbox API proxy.
            item.pop("use_proxy", None)
            item["api_base"] = str(item.get("api_base") or "").strip().rstrip("/")
            # A provider card can be switched from GptMail to TempMail.lol. Do
            # not carry GptMail's endpoint into TempMail.lol, whose official
            # endpoint is used automatically when this field is blank.
            if item["type"] == "tempmail_lol":
                if "mail.chatgpt.org.uk" in item["api_base"].lower():
                    item["api_base"] = ""
                # Persist the official endpoint so the UI always shows the
                # effective default while still allowing a compatible custom URL.
                if not item["api_base"]:
                    item["api_base"] = "https://api.tempmail.lol/v2"
            elif item["type"] == "gptmail" and "api.tempmail.lol" in item["api_base"].lower():
                # A TempMail.lol endpoint cannot answer GptMail's /api/* API.
                # Clear it so validation reports the missing GptMail settings
                # instead of failing later with a misleading JSON parse error.
                item["api_base"] = ""
            raw_domains = item.get("domains", item.get("domain", []))
            if isinstance(raw_domains, str):
                raw_domains = [part.strip() for part in raw_domains.split(",")]
            item["domains"] = [str(domain).strip() for domain in (raw_domains or []) if str(domain).strip()]
            item.pop("domain", None)
            secret = str(item.get("api_key") or "").strip()
            if not secret:
                secret = str(old_by_id.get(item["id"], {}).get("api_key") or "")
            item["api_key"] = secret
            item.pop("api_key_configured", None)
            if item["type"] not in {"gptmail", "tempmail_lol", "outlook_token"}:
                raise ValueError("Supported mailbox provider types: gptmail, tempmail_lol, outlook_token")
            mailbox_text = str(item.get("mailboxes") or "")
            old_mailboxes = str(old_by_id.get(item["id"], {}).get("mailboxes") or "")
            if item["type"] == "outlook_token":
                if not mailbox_text.strip():
                    mailbox_text = old_mailboxes
                else:
                    from .mail import parse_outlook_credentials
                    merged: dict[str, dict[str, str]] = {credential["email"].lower(): credential for credential in parse_outlook_credentials(old_mailboxes)}
                    for credential in parse_outlook_credentials(mailbox_text):
                        merged[credential["email"].lower()] = credential
                    mailbox_text = "\n".join(f'{credential["email"]}----{credential.get("password", "")}----{credential["client_id"]}----{credential["refresh_token"]}' for credential in merged.values())
            item["mailboxes"] = mailbox_text
            item.pop("mailboxes_configured", None)
            item.pop("mailboxes_count", None)
            item.pop("mailboxes_base_count", None)
            item.pop("mailboxes_alias_count", None)
            item.pop("mailboxes_stats", None)
            item.pop("mailboxes_parse_stats", None)
            if item["type"] == "outlook_token":
                item["mode"] = str(item.get("mode") or "auto").strip().lower()
                if item["mode"] not in {"graph", "imap", "auto"}:
                    item["mode"] = "auto"
                item["imap_host"] = str(item.get("imap_host") or "outlook.office365.com").strip() or "outlook.office365.com"
                try:
                    item["message_limit"] = max(1, min(100, int(item.get("message_limit") or 10)))
                    item["alias_per_email"] = max(0, min(200, int(item.get("alias_per_email") or 0)))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Microsoft ????????????????") from exc
                item["alias_enabled"] = bool(item.get("alias_enabled", False))
                item["alias_include_original"] = bool(item.get("alias_include_original", True))
                item["alias_prefix"] = re.sub(r"[^A-Za-z0-9._-]+", "", str(item.get("alias_prefix") or "c2api").strip()) or "c2api"
                item["preflight_enabled"] = bool(item.get("preflight_enabled", True))
            else:
                for field in ("mode", "imap_host", "message_limit", "alias_enabled", "alias_per_email", "alias_prefix", "alias_include_original", "preflight_enabled"):
                    item.pop(field, None)
            if item["type"] == "gptmail" and item["enabled"] and (not item["api_base"] or not item["api_key"]):
                raise ValueError(f"GptMail provider {item['name']} requires an API base URL and API key")
            if item["type"] == "outlook_token" and item["enabled"]:
                from .mail import parse_outlook_credentials
                if not parse_outlook_credentials(mailbox_text):
                    raise ValueError("Microsoft ????????????????email----password----client_id----refresh_token")
            normalized.append(item)
        run = incoming.setdefault("run", {})
        try:
            run["count"] = int(run.get("count") or 1)
            run["mailbox_attempts"] = int(run.get("mailbox_attempts") or 5)
            run["code_timeout_sec"] = int(run.get("code_timeout_sec") or 120)
        except (TypeError, ValueError) as exc:
            raise ValueError("注册数量、邮箱尝试次数和验证码等待时间必须为整数") from exc
        if not 1 <= run["count"] <= 100:
            raise ValueError("单次注册数量应在 1 到 100 之间")
        if not 1 <= run["mailbox_attempts"] <= 10:
            raise ValueError("单账号邮箱尝试次数应在 1 到 10 之间")
        if not 30 <= run["code_timeout_sec"] <= 600:
            raise ValueError("验证码等待时间应在 30 到 600 秒之间")
        cpa = incoming.setdefault("cpa", {})
        cpa["enabled"] = bool(cpa.get("enabled", False))
        cpa["copy_to_hotload"] = bool(cpa.get("copy_to_hotload", False))
        cpa["prefer_protocol"] = bool(cpa.get("prefer_protocol", True))
        cpa["protocol_only"] = bool(cpa.get("protocol_only", False))
        cpa["probe_after_write"] = bool(cpa.get("probe_after_write", True))
        cpa["probe_chat"] = bool(cpa.get("probe_chat", False))
        cpa["probe_required"] = bool(cpa.get("probe_required", False))
        cpa["mint_required"] = bool(cpa.get("mint_required", False))
        for key, default in (("auth_dir", ""), ("hotload_dir", ""), ("base_url", "https://cli-chat-proxy.grok.com/v1")):
            cpa[key] = str(cpa.get(key) or default).strip()
        supplied_cpa_proxy = str(cpa.get("proxy") or "").strip()
        if not supplied_cpa_proxy:
            supplied_cpa_proxy = str((existing.get("cpa") or {}).get("proxy") or "").strip()
        cpa["proxy"] = supplied_cpa_proxy
        cpa.pop("proxy_configured", None)
        try:
            cpa["protocol_poll_timeout_sec"] = int(cpa.get("protocol_poll_timeout_sec") or 90)
            cpa["timeout_sec"] = int(cpa.get("timeout_sec") or 300)
            cpa["mint_gap_sec"] = float(cpa.get("mint_gap_sec") or 25)
            cpa["drain_timeout_sec"] = int(cpa.get("drain_timeout_sec") or 600)
        except (TypeError, ValueError) as exc:
            raise ValueError("CPA 协议轮询、mint 超时和间隔必须为数字") from exc
        if not 30 <= cpa["protocol_poll_timeout_sec"] <= 600:
            raise ValueError("CPA 协议轮询超时应在 30 到 600 秒之间")
        if not 60 <= cpa["timeout_sec"] <= 900:
            raise ValueError("CPA mint 超时应在 60 到 900 秒之间")
        if not 0 <= cpa["mint_gap_sec"] <= 600:
            raise ValueError("CPA mint 间隔应在 0 到 600 秒之间")

        account = incoming.setdefault("account", {})
        account["pool"] = str(account.get("pool") or "basic").strip().lower()
        account["tags"] = [str(tag).strip() for tag in account.get("tags") or [] if str(tag).strip()]
        incoming["proxy"] = str(incoming.get("proxy") or "").strip()
        incoming["browser_proxy"] = str(incoming.get("browser_proxy") or "").strip()
        mail["providers"] = normalized
        self._settings_path.write_text(json.dumps(incoming, ensure_ascii=False, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(self._settings_path, 0o600)
        return self.get_settings()

    @staticmethod
    def _serialize_outlook_credentials(credentials: list[dict[str, str]]) -> str:
        return "\n".join(
            f'{credential["email"]}----{credential.get("password", "")}----{credential["client_id"]}----{credential["refresh_token"]}'
            for credential in credentials
        )

    def outlook_pool_details(self, provider_id: str, status: str = "all") -> dict[str, Any]:
        from .mail import outlook_pool_entries, parse_outlook_credentials

        requested_id = str(provider_id or "").strip()
        for provider in (self._read_settings_raw().get("mail") or {}).get("providers") or []:
            if isinstance(provider, dict) and provider.get("type") == "outlook_token" and str(provider.get("id") or "") == requested_id:
                rows = outlook_pool_entries(parse_outlook_credentials(str(provider.get("mailboxes") or "")), provider, status)
                return {"provider_id": requested_id, "status": str(status or "all").strip().lower(), "count": len(rows), "items": rows}
        raise ValueError("Microsoft ????????")

    def reset_outlook_pool(self, scope: str = "all") -> dict[str, Any]:
        """Maintain local Microsoft mailbox pool state without returning credentials."""
        from .mail import parse_outlook_credentials, prune_outlook_unused_credentials, remove_outlook_invalid_credentials, reset_outlook_pool_state

        normalized_scope = str(scope or "all").strip().lower()
        aliases = {"failed": "retryable", "retryable": "retryable", "busy": "busy", "invalid": "invalid", "used": "used", "unused": "unused", "delete_invalid": "delete_invalid", "all": "all"}
        normalized_scope = aliases.get(normalized_scope, "all")
        if normalized_scope in {"unused", "delete_invalid"}:
            data = self._read_settings_raw()
            removed = 0
            for provider in (data.get("mail") or {}).get("providers") or []:
                if not isinstance(provider, dict) or provider.get("type") != "outlook_token":
                    continue
                credentials = parse_outlook_credentials(str(provider.get("mailboxes") or ""))
                if normalized_scope == "unused":
                    kept, count = prune_outlook_unused_credentials(credentials, provider)
                else:
                    kept, count = remove_outlook_invalid_credentials(credentials, provider)
                if count:
                    provider["mailboxes"] = self._serialize_outlook_credentials(kept)
                    removed += count
            if removed:
                self._settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            cleared = removed
        else:
            cleared = reset_outlook_pool_state(normalized_scope)
        result = self.get_settings()
        result["outlook_pool_reset"] = {"scope": normalized_scope, "cleared": cleared}
        return result

    def status(self) -> dict[str, Any]:
        state = dict(self._runtime)
        state["running"] = state["state"] == "running"
        state["lines"] = list(self._lines)
        return state

    async def start(self, *, admin_key: str, server_port: int) -> dict[str, Any]:
        async with self._lock:
            if self._process and self._process.returncode is None:
                raise RuntimeError("已有注册任务正在运行")
            settings = self._read_settings_raw()
            providers = [p for p in (settings.get("mail") or {}).get("providers") or [] if p.get("enabled", True)]
            if not providers:
                raise RuntimeError("请先保存并启用至少一个邮箱服务")
            invalid_gptmail = [
                p for p in providers
                if str(p.get("type") or "gptmail").strip().lower() == "gptmail"
                and (not p.get("api_base") or not p.get("api_key"))
            ]
            if invalid_gptmail:
                raise RuntimeError("Enabled GptMail providers require an API base URL and API key")


            task_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
            task_started_at = _now()
            run_dir = self._dir / "runs"
            run_dir.mkdir(parents=True, exist_ok=True)
            payload = copy.deepcopy(settings)
            payload.setdefault("cpa", {})["task_id"] = task_id
            initialize_cpa_task(
                payload["cpa"],
                task_id,
                started_at=task_started_at,
                requested_count=int((payload.get("run") or {}).get("count") or 0),
            )
            payload["api"] = {
                "endpoint": f"http://127.0.0.1:{server_port}/admin/api/registration/archive/import",
                "token": admin_key,
            }
            config_path = run_dir / f"{task_id}.json"
            config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(config_path, 0o600)

            logs_dir = log_path("registration")
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_file = logs_dir / f"{task_id}.log"
            self._lines.clear()
            self._runtime = {
                "state": "running", "task_id": task_id, "started_at": task_started_at, "finished_at": None,
                "exit_code": None, "message": "浏览器注册任务正在运行", "log_file": str(log_file),
            }
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", "-m", "app.control.registration.runner", "--config", str(config_path),
                    cwd=str(Path(__file__).resolve().parents[3]),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except Exception:
                config_path.unlink(missing_ok=True)
                raise
            self._process = process
            self._watch_task = asyncio.create_task(
                self._watch_process(process, log_file, config_path, task_id, payload["cpa"]), name=f"registration-{task_id}"
            )
            return self.status()

    async def _watch_process(
        self,
        process: asyncio.subprocess.Process,
        log_file: Path,
        config_path: Path,
        task_id: str,
        cpa: dict[str, Any],
    ) -> None:
        if process.stdout is None:
            config_path.unlink(missing_ok=True)
            return
        try:
            with log_file.open("a", encoding="utf-8") as handle:
                while line := await process.stdout.readline():
                    text = line.decode("utf-8", errors="replace").rstrip()
                    if not text:
                        continue
                    self._lines.append(text)
                    handle.write(f"{_now()} | {text}\n")
                    handle.flush()
            code = await process.wait()
            if self._runtime.get("state") != "cancelled":
                self._runtime.update({
                    "state": "completed" if code == 0 else "failed",
                    "finished_at": _now(), "exit_code": code,
                    "message": "注册任务已完成" if code == 0 else f"注册任务异常退出（exit={code}）",
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._runtime.update({"state": "failed", "finished_at": _now(), "message": f"任务监控异常: {exc}"})
        finally:
            # The one-off payload contains mailbox credentials and the internal
            # admin token used for automatic account-pool import.  The runner
            # has already loaded it, so it must not remain in the persistent
            # DATA_DIR after the task ends.
            with contextlib.suppress(OSError):
                config_path.unlink(missing_ok=True)
            with contextlib.suppress(Exception):
                finalize_cpa_task(
                    cpa,
                    task_id,
                    state=str(self._runtime.get("state") or "completed"),
                    finished_at=str(self._runtime.get("finished_at") or _now()),
                )
            if self._process is process:
                self._process = None
            if self._watch_task is asyncio.current_task():
                self._watch_task = None

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            process = self._process
            if not process or process.returncode is not None:
                raise RuntimeError("当前没有正在运行的注册任务")
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=12)
            except TimeoutError:
                process.kill()
                await process.wait()
            self._runtime.update({"state": "cancelled", "finished_at": _now(), "exit_code": process.returncode, "message": "注册任务已停止"})
            return self.status()

    async def shutdown(self) -> None:
        if self._process and self._process.returncode is None:
            with contextlib.suppress(Exception):
                await self.stop()
        if self._watch_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._watch_task


__all__ = ["RegistrationManager"]
