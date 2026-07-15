"""Mailbox providers used by the integrated browser registration worker."""
from __future__ import annotations

import json
import random
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover - Docker image supplies curl_cffi
    curl_requests = None

import requests


class VerificationCodeTimeout(RuntimeError):
    """The current mailbox did not receive an xAI verification code in time."""


class MailboxRateLimited(RuntimeError):
    """A mailbox API asked the worker to stop polling for a short period."""

    def __init__(self, provider_name: str, retry_after: float | None = None) -> None:
        self.provider_name = provider_name
        self.retry_after = max(0.0, float(retry_after or 0.0))
        wait_text = f"; retry after {self.retry_after:.0f}s" if self.retry_after else ""
        super().__init__(f"{provider_name} inbox rate limited{wait_text}")


@dataclass(frozen=True)
class Mailbox:
    address: str
    provider_index: int
    provider_token: str = ""

    def token(self) -> str:
        return json.dumps({"provider_index": self.provider_index, "provider_token": self.provider_token}, separators=(",", ":"))


class _MailboxProvider:
    name: str
    poll_interval_seconds: float = 3.0

    def create_mailbox(self) -> str | dict[str, str]:
        raise NotImplementedError

    def list_messages(self, address: str, provider_token: str = "") -> list[dict[str, Any]]:
        raise NotImplementedError

    def message_content(self, message_id: str, provider_token: str = "") -> str:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


def _create_session(proxy: str = ""):
    session = curl_requests.Session(impersonate="chrome136") if curl_requests else requests.Session()
    if proxy.strip():
        url = proxy.strip() if "://" in proxy else f"http://{proxy.strip()}"
        session.proxies.update({"http": url, "https": url})
    return session


class GptMailProvider(_MailboxProvider):
    """Adapter for the GptMail-compatible API used by grok-register-main."""

    def __init__(self, entry: dict[str, Any], proxy: str = "") -> None:
        self.name = str(entry.get("name") or "GptMail")
        self.base_url = str(entry.get("api_base") or "").rstrip("/")
        self.api_key = str(entry.get("api_key") or "")
        if not self.base_url or not self.api_key:
            raise RuntimeError(f"Mailbox provider {self.name} requires an API base URL and API key")
        self.session = _create_session(proxy)

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json, text/plain, */*", "X-API-Key": self.api_key, "Origin": self.base_url, "Referer": f"{self.base_url}/"}

    def create_mailbox(self) -> str:
        response = self.session.get(f"{self.base_url}/api/generate-email", headers=self._headers(), timeout=20)
        response.raise_for_status()
        body = response.json()
        address = str((body.get("data") or {}).get("email") or "").strip()
        if not body.get("success") or not address:
            raise RuntimeError(str(body.get("error") or f"{self.name} did not return an email address"))
        return address

    def list_messages(self, address: str, provider_token: str = "") -> list[dict[str, Any]]:
        response = self.session.get(f"{self.base_url}/api/emails", params={"email": address}, headers=self._headers(), timeout=30)
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            return []
        messages = (body.get("data") or {}).get("emails") or []
        return [item for item in messages if isinstance(item, dict)]

    def message_content(self, message_id: str, provider_token: str = "") -> str:
        response = self.session.get(f"{self.base_url}/api/email/{message_id}", headers=self._headers(), timeout=30)
        response.raise_for_status()
        body = response.json()
        if not body.get("success"):
            return ""
        data = body.get("data") or {}
        return str(data.get("content") or data.get("html_content") or "")

    def close(self) -> None:
        self.session.close()


class TempMailLolProvider(_MailboxProvider):
    """TempMail.lol v2 inbox API, modelled after chatgpt2api's provider."""

    def __init__(self, entry: dict[str, Any], proxy: str = "") -> None:
        self.name = str(entry.get("name") or "TempMail.lol")
        self.base_url = str(entry.get("api_base") or "https://api.tempmail.lol/v2").rstrip("/")
        self.api_key = str(entry.get("api_key") or "").strip()
        raw_domains = entry.get("domains", entry.get("domain", []))
        if isinstance(raw_domains, str):
            raw_domains = [part.strip() for part in raw_domains.split(",")]
        self.domains = [str(item).strip() for item in (raw_domains or []) if str(item).strip()]
        # Browser traffic needs the configured WARP/Privoxy route for xAI, but
        # TempMail.lol can rate-limit that shared egress IP.  Keep mailbox API
        # requests direct by default; an explicit provider setting can opt in.
        self.use_proxy = bool(entry.get("use_proxy", False))
        self.session = _create_session(proxy if self.use_proxy else "")
        # TempMail.lol limits polling more aggressively than GptMail.  Keep this
        # conservative even before a 429 is observed.
        self.poll_interval_seconds = 10.0
        self._inbox_cooldown_until = 0.0
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        if self.api_key:
            self.session.headers["Authorization"] = f"Bearer {self.api_key}"

    @staticmethod
    def _random_prefix() -> str:
        alphabet = "abcdefghijklmnopqrstuvwxyz0123456789"
        return "grok" + "".join(secrets.choice(alphabet) for _ in range(10))

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, payload: dict[str, Any] | None = None, expected: tuple[int, ...] = (200,)) -> dict[str, Any]:
        # The service applies the limit per egress IP.  Honour a previously
        # received cooldown rather than immediately issuing another request.
        if path == "/inbox":
            remaining = self._inbox_cooldown_until - time.monotonic()
            if remaining > 0:
                raise MailboxRateLimited(self.name, remaining)

        response = self.session.request(method.upper(), f"{self.base_url}{path}", params=params, json=payload, timeout=30, verify=False)
        if response.status_code == 429:
            retry_after = 0.0
            raw_retry_after = str(response.headers.get("Retry-After") or "").strip()
            try:
                retry_after = float(raw_retry_after)
            except ValueError:
                # TempMail.lol does not always provide Retry-After.  A 15s
                # default prevents the former 3-second retry storm.
                retry_after = 15.0
            retry_after = min(max(retry_after, 10.0), 60.0)
            if path == "/inbox":
                self._inbox_cooldown_until = max(self._inbox_cooldown_until, time.monotonic() + retry_after)
            raise MailboxRateLimited(self.name, retry_after)
        if response.status_code not in expected:
            raise RuntimeError(f"TempMail.lol request failed: {method} {path}, HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError(f"TempMail.lol {method} {path} returned a non-object payload")
        return data

    def create_mailbox(self) -> dict[str, str]:
        payload: dict[str, Any] = {}
        if self.domains:
            domain = random.choice(self.domains).lower()
            if domain.startswith("*."):
                domain = f"{self._random_prefix()}.{domain[2:]}"
                payload["prefix"] = self._random_prefix()
            payload["domain"] = domain
        data = self._request("POST", "/inbox/create", payload=payload, expected=(200, 201))
        address = str(data.get("address") or "").strip()
        token = str(data.get("token") or "").strip()
        if not address or not token:
            raise RuntimeError("TempMail.lol response is missing address or token")
        return {"address": address, "token": token}

    @staticmethod
    def _message_id(item: dict[str, Any]) -> str:
        return str(item.get("id") or item.get("token") or item.get("message_id") or "")

    def list_messages(self, address: str, provider_token: str = "") -> list[dict[str, Any]]:
        if not provider_token:
            raise RuntimeError("TempMail.lol mailbox context is missing an inbox token")
        data = self._request("GET", "/inbox", params={"token": provider_token})
        items = data.get("emails") or data.get("messages") or []
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def message_content(self, message_id: str, provider_token: str = "") -> str:
        for item in self.list_messages("", provider_token):
            if self._message_id(item) != message_id:
                continue
            values: list[str] = []
            for key in ("content", "text", "text_content", "body", "html", "html_content"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    values.append(value)
                elif isinstance(value, dict):
                    values.extend(str(part) for part in value.values() if isinstance(part, str) and part)
            return "\n".join(values)
        return ""

    def close(self) -> None:
        self.session.close()


class MailboxPool:
    def __init__(self, providers: list[dict[str, Any]], proxy: str = "") -> None:
        enabled = [p for p in providers if bool(p.get("enabled", True))]
        if not enabled:
            raise RuntimeError("At least one mailbox provider must be enabled")
        factories = {"gptmail": GptMailProvider, "tempmail_lol": TempMailLolProvider}
        self._providers: list[_MailboxProvider] = []
        unsupported: list[str] = []
        for entry in enabled:
            provider_type = str(entry.get("type") or "gptmail").strip().lower()
            factory = factories.get(provider_type)
            if factory is None:
                unsupported.append(provider_type)
                continue
            self._providers.append(factory(entry, proxy))
        if unsupported:
            raise RuntimeError(f"Unsupported mailbox provider types: {', '.join(unsupported)}")
        self._cursor = random.randrange(len(self._providers))

    def acquire(self) -> Mailbox:
        errors: list[str] = []
        for offset in range(len(self._providers)):
            index = (self._cursor + offset) % len(self._providers)
            provider = self._providers[index]
            try:
                created = provider.create_mailbox()
                if isinstance(created, dict):
                    address, provider_token = str(created.get("address") or ""), str(created.get("token") or "")
                else:
                    address, provider_token = str(created), ""
                if not address:
                    raise RuntimeError("Mailbox provider did not return an address")
                self._cursor = (index + 1) % len(self._providers)
                print(f"[mail] {provider.name} allocated mailbox: {address}", flush=True)
                return Mailbox(address=address, provider_index=index, provider_token=provider_token)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
        raise RuntimeError("All mailbox providers failed to allocate an address; " + "; ".join(errors))

    @staticmethod
    def _inline_message_content(item: dict[str, Any]) -> str:
        values: list[str] = []
        for key in ("content", "text", "text_content", "body", "html", "html_content"):
            value = item.get(key)
            if isinstance(value, str) and value:
                values.append(value)
            elif isinstance(value, dict):
                values.extend(str(part) for part in value.values() if isinstance(part, str) and part)
        return "\n".join(values)

    @staticmethod
    def _sleep_until_next_poll(deadline: float, seconds: float) -> bool:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(max(seconds, 0.0), remaining))
        return time.monotonic() < deadline

    def wait_for_code(self, address: str, token: str, timeout: int = 120) -> str:
        try:
            context = json.loads(token)
            provider = self._providers[int(context.get("provider_index"))]
            provider_token = str(context.get("provider_token") or "")
        except Exception as exc:
            raise RuntimeError("Mailbox context was lost") from exc

        deadline = time.monotonic() + timeout
        poll_interval = max(3.0, float(getattr(provider, "poll_interval_seconds", 3.0)))
        old_ids: set[str] = set()
        next_wait = 0.0

        try:
            existing = provider.list_messages(address, provider_token)
            old_ids = {str(item.get("id") or item.get("token") or item.get("message_id") or "") for item in existing}
            for item in existing:
                inline = self._inline_message_content(item)
                code = extract_verification_code(inline) or extract_verification_code(str(item.get("subject") or ""))
                if code:
                    print(f"[mail] verification code received: {code}", flush=True)
                    return code
            # Do not immediately re-read the same inbox after the baseline check.
            next_wait = poll_interval
        except MailboxRateLimited as exc:
            next_wait = max(poll_interval, exc.retry_after or 15.0)
            print(f"[mail] {exc.provider_name} inbox rate limited; waiting {next_wait:.0f}s before retry", flush=True)
        except Exception as exc:
            next_wait = poll_interval
            print(f"[mail] initial inbox check failed: {type(exc).__name__}: {exc}", flush=True)

        print(f"[mail] waiting for {address} verification code, timeout {timeout}s", flush=True)
        while self._sleep_until_next_poll(deadline, next_wait):
            try:
                for item in provider.list_messages(address, provider_token):
                    message_id = str(item.get("id") or item.get("token") or item.get("message_id") or "")
                    if not message_id or message_id in old_ids:
                        continue
                    inline = self._inline_message_content(item)
                    code = extract_verification_code(inline) or extract_verification_code(str(item.get("subject") or ""))
                    if not code:
                        # GptMail only returns the full body from its detail API;
                        # TempMail.lol normally has it inline, avoiding a second
                        # /inbox request for every polling iteration.
                        content = provider.message_content(message_id, provider_token)
                        code = extract_verification_code(content)
                    if code:
                        print(f"[mail] verification code received: {code}", flush=True)
                        return code
                    # The message is immutable and not a verification email;
                    # do not fetch its detail again on every later inbox poll.
                    old_ids.add(message_id)
                next_wait = poll_interval
            except MailboxRateLimited as exc:
                next_wait = max(poll_interval, exc.retry_after or 15.0)
                print(f"[mail] {exc.provider_name} inbox rate limited; waiting {next_wait:.0f}s before retry", flush=True)
            except Exception as exc:
                next_wait = poll_interval
                print(f"[mail] inbox polling failed: {type(exc).__name__}: {exc}", flush=True)
        raise VerificationCodeTimeout("Timed out waiting for a verification code")

    def close(self) -> None:
        for provider in self._providers:
            provider.close()


def extract_verification_code(content: str) -> str | None:
    # Mail bodies often contain CSS such as `text-size` inside <style> blocks.
    # Strip non-visible script/style content before looking for an ABC-123 code,
    # otherwise CSS identifiers can be submitted as a verification code.
    clean = re.sub(r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>", " ", content or "", flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<[^>]+>", " ", clean)
    clean = re.sub(r"\s+", " ", clean)
    contextual = re.search(r"(?:code|confirmation|validate|below)[:\s]+([A-Z0-9]{3,4}-[A-Z0-9]{3,4})", clean, re.I)
    if contextual:
        return contextual.group(1).upper()
    hyphenated = re.search(r"(?<![&#])\b([A-Z0-9]{3,4}-[A-Z0-9]{3,4})\b", clean, re.I)
    if hyphenated:
        return hyphenated.group(1).upper()
    numeric = re.search(r"(?<![#&])\b(\d{6})\b", clean)
    return numeric.group(1) if numeric else None


__all__ = ["Mailbox", "MailboxPool", "MailboxRateLimited", "TempMailLolProvider", "VerificationCodeTimeout", "extract_verification_code"]
