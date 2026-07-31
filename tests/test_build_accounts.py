from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orjson

from app.control.build.accounts import (
    BUILD_STATUS_ACTIVE,
    BUILD_STATUS_COOLING,
    BUILD_STATUS_DISABLED,
    BuildAccountStore,
    refresh_account,
)
from app.platform.errors import UpstreamError


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, body: str = "") -> None:
        self.status_code = status_code
        self.content = orjson.dumps(payload) if payload is not None else body.encode("utf-8")
        self.headers: dict[str, str] = {}


class _FakeSession:
    response = _FakeResponse(200, {"access_token": "new.access.token", "refresh_token": "new-refresh", "expires_in": 3600})

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, *_args, **_kwargs):
        return self.response


class _FakeProxy:
    async def acquire(self):
        return object()


class BuildAccountStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = BuildAccountStore()
        self.store._path = Path(self.temp.name) / "build_accounts.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _import_one(self, email: str = "build@example.test") -> str:
        result = self.store.upsert_payloads(
            [{
                "email": email,
                "user_id": f"user-{email}",
                "access_token": "old.access.token",
                "refresh_token": "old-refresh",
                "expires_at": "2099-01-01T00:00:00Z",
            }],
            model_overrides={f"user-{email}": ["grok-4.5"]},
        )
        self.assertEqual(result["added"], 1)
        return next(item["id"] for item in self.store.list(include_secrets=True) if item["email"] == email)

    def test_legacy_json_defaults_to_active_or_manual_disabled(self) -> None:
        self.store._path.write_bytes(orjson.dumps({"accounts": [
            {"id": "one", "source_key": "one", "access_token": "a", "refresh_token": "r", "enabled": True},
            {"id": "two", "source_key": "two", "access_token": "a2", "refresh_token": "r2", "enabled": False},
        ]}))
        rows = {item["id"]: item for item in self.store.list()}
        self.assertEqual(rows["one"]["status"], BUILD_STATUS_ACTIVE)
        self.assertEqual(rows["two"]["status"], "manual_disabled")
        self.assertNotIn("access_token", rows["one"])
        self.assertNotIn("refresh_token", rows["one"])
        self.assertNotIn("headers", rows["one"])

    def test_429_enters_cooling_and_auto_recovers(self) -> None:
        account_id = self._import_one()
        lease = self.store.reserve("grok-4.5")
        err = UpstreamError("rate limited", status=429)
        err.details["retry_after"] = "2"
        self.store.release(lease, success=False, error=err)
        row = self.store.list()[0]
        self.assertEqual(row["status"], BUILD_STATUS_COOLING)
        self.assertEqual(row["state_reason"], "rate_limited")
        self.assertGreater(row["cooldown_remaining_ms"], 0)
        with self.assertRaises(Exception):
            self.store.reserve("grok-4.5")

        expired = self.store.get(account_id)
        assert expired is not None
        expired.cooldown_until = 1
        self.store.update(expired)
        row = self.store.list()[0]
        self.assertEqual(row["status"], BUILD_STATUS_ACTIVE)
        self.assertEqual(row["cooldown_remaining_ms"], 0)
        self.store.reserve("grok-4.5")

    def test_concurrent_success_does_not_clear_rate_limit_cooldown(self) -> None:
        self._import_one()
        first = self.store.reserve("grok-4.5")
        second = self.store.reserve("grok-4.5")
        error = UpstreamError("rate limited", status=429)
        error.details["retry_after"] = "30"
        self.store.release(first, success=False, error=error)
        self.store.release(second, success=True)
        row = self.store.list()[0]
        self.assertEqual(row["status"], BUILD_STATUS_COOLING)
        self.assertEqual(row["state_reason"], "rate_limited")
        self.assertGreater(row["cooldown_remaining_ms"], 0)

    def test_oauth_invalid_grant_disables_but_transient_error_only_cools(self) -> None:
        account_id = self._import_one()
        invalid = UpstreamError("Grok Build OAuth refresh failed", status=400, body="invalid_grant")
        invalid.details["build_oauth_refresh"] = True
        self.store.record_failure(account_id, invalid)
        row = self.store.list()[0]
        self.assertEqual(row["status"], BUILD_STATUS_DISABLED)
        self.assertEqual(row["state_reason"], "oauth_refresh_token_invalid")

        account_id = self._import_one("temporary@example.test")
        transient = UpstreamError("Grok Build response transport failed", status=502)
        self.store.record_failure(account_id, transient)
        second = next(item for item in self.store.list() if item["id"] == account_id)
        self.assertEqual(second["status"], BUILD_STATUS_COOLING)
        self.assertEqual(second["state_reason"], "transient_upstream_failure")

    def test_authorization_rejected_after_forced_refresh_disables(self) -> None:
        account_id = self._import_one()
        error = UpstreamError("Grok Build response failed", status=401)
        error.details["build_auth_rechecked"] = True
        self.store.record_failure(account_id, error)
        row = self.store.list()[0]
        self.assertEqual(row["status"], BUILD_STATUS_DISABLED)
        self.assertEqual(row["state_reason"], "build_authorization_rejected")

    async def test_admin_list_filters_and_paginates_without_secrets(self) -> None:
        from app.products.web.admin import build as build_admin

        self._import_one("first@example.test")
        second_id = self._import_one("second@example.test")
        disabled = UpstreamError("Grok Build OAuth refresh failed", status=400, body="invalid_grant")
        disabled.details["build_oauth_refresh"] = True
        self.store.record_failure(second_id, disabled)
        with patch.object(build_admin, "store", self.store):
            result = await build_admin.list_accounts(page=1, page_size=10, query="second", status="disabled")
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual(item["id"], second_id)
        self.assertEqual(item["status"], BUILD_STATUS_DISABLED)
        self.assertNotIn("access_token", item)
        self.assertNotIn("refresh_token", item)
        self.assertNotIn("headers", item)

    def test_reimport_keeps_manual_stop_but_repairs_runtime_state(self) -> None:
        account_id = self._import_one()
        self.store.set_enabled(account_id, False)
        self.store.upsert_payloads(
            [{"email": "build@example.test", "user_id": "user-build@example.test", "access_token": "fresh.token", "refresh_token": "fresh-refresh"}],
            model_overrides={"user-build@example.test": ["grok-4.5"]},
        )
        row = self.store.list()[0]
        self.assertEqual(row["status"], "manual_disabled")
        self.assertFalse(row["enabled"])
        self.assertEqual(row["runtime_status"], BUILD_STATUS_ACTIVE)

    def test_reimported_cpa_auth_resets_runtime_disabled_state(self) -> None:
        account_id = self._import_one()
        invalid = UpstreamError("Grok Build OAuth refresh failed", status=400, body="invalid_grant")
        invalid.details["build_oauth_refresh"] = True
        self.store.record_failure(account_id, invalid)
        self.assertEqual(self.store.list()[0]["status"], BUILD_STATUS_DISABLED)
        self.store.upsert_payloads(
            [{"email": "build@example.test", "user_id": "user-build@example.test", "access_token": "fresh.token", "refresh_token": "fresh-refresh"}],
            model_overrides={"user-build@example.test": ["grok-4.5"]},
        )
        row = self.store.list()[0]
        self.assertEqual(row["status"], BUILD_STATUS_ACTIVE)
        self.assertEqual(row["state_reason"], "")

    async def test_refresh_account_persists_rotated_tokens_and_expiry(self) -> None:
        account_id = self._import_one()
        original = self.store.get(account_id)
        assert original is not None
        with (
            patch("app.control.build.accounts.store", self.store),
            patch("app.control.build.accounts.get_proxy_runtime", return_value=_FakeProxy()),
            patch("app.control.build.accounts.build_session_kwargs", return_value={}),
            patch("app.control.build.accounts.ResettableSession", _FakeSession),
        ):
            refreshed = await refresh_account(original, force=True)
        self.assertEqual(refreshed.access_token, "new.access.token")
        self.assertEqual(refreshed.refresh_token, "new-refresh")
        self.assertGreater(refreshed.expires_at, 0)
        self.assertGreater(refreshed.last_refresh_at, 0)
        persisted = self.store.get(account_id)
        assert persisted is not None
        self.assertEqual(persisted.refresh_token, "new-refresh")


if __name__ == "__main__":
    unittest.main()
