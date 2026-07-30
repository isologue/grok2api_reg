import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.account.backends.local import LocalAccountRepository
from app.control.account.commands import AccountPatch, AccountUpsert
from app.control.account.enums import AccountStatus, FeedbackKind
from app.control.account.refresh import AccountRefreshService, reconcile_legacy_rate_limited_accounts
from app.control.account.invalid_credentials import feedback_kind_for_error
from app.control.account.models import AccountRecord
from app.control.account.state_machine import AccountFeedback, StatePolicy, apply_feedback
from app.platform.errors import UpstreamError
from app.products._account_selection import selection_max_retries


_ROOT = Path(__file__).resolve().parents[1]


class AccountErrorClassificationTests(unittest.TestCase):
    def test_invalid_credentials_marker_overrides_generic_403(self):
        exc = UpstreamError("blocked", status=403, body="blocked-user")
        self.assertEqual(feedback_kind_for_error(exc), FeedbackKind.UNAUTHORIZED)

    def test_plain_403_is_forbidden_and_not_expired_marker(self):
        exc = UpstreamError("challenge", status=403, body="cf challenge")
        self.assertEqual(feedback_kind_for_error(exc), FeedbackKind.FORBIDDEN)

    def test_transport_error_does_not_burn_account_health(self):
        exc = UpstreamError("chat transport error: connect timeout")
        self.assertEqual(feedback_kind_for_error(exc), FeedbackKind.TRANSPORT_ERROR)


class AccountErrorStateTests(unittest.TestCase):
    def test_generic_403_enters_temporary_cooling_instead_of_disabled(self):
        record = AccountRecord(token="test-token")
        result = apply_feedback(
            record,
            AccountFeedback(
                kind=FeedbackKind.FORBIDDEN,
                at=1_000,
                reason="forbidden_or_challenge",
            ),
            policy=StatePolicy(forbidden_cooling_ms=60_000),
        )

        self.assertEqual(result.status, AccountStatus.COOLING)
        self.assertEqual(result.ext["cooldown_until"], 61_000)
        self.assertNotEqual(result.status, AccountStatus.DISABLED)

    def test_transport_error_does_not_increment_account_failure_counter(self):
        record = AccountRecord(token="test-token")
        result = apply_feedback(
            record,
            AccountFeedback(kind=FeedbackKind.TRANSPORT_ERROR, at=1_000),
        )

        self.assertEqual(result.status, AccountStatus.ACTIVE)
        self.assertEqual(result.usage_fail_count, 0)




class AccountFailurePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_403_is_persisted_as_temporary_cooling(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="test-token")])
            service = AccountRefreshService(repo)

            with patch(
                "app.control.account.refresh.get_config", return_value=60
            ):
                await service.record_failure_async(
                    "test-token", 0, UpstreamError("challenge", status=403)
                )

            record = next(iter(await repo.get_accounts(["test-token"])))

        self.assertEqual(record.status, AccountStatus.COOLING)
        self.assertEqual(record.last_fail_reason, "forbidden_or_challenge")
        self.assertEqual(record.state_reason, "forbidden_or_challenge")
        self.assertGreater(record.ext.get("cooldown_until", 0), 0)

    async def test_plain_401_is_persisted_as_expired(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="test-token")])
            service = AccountRefreshService(repo)
            await service.record_failure_async(
                "test-token", 0, UpstreamError("unauthorized", status=401)
            )
            record = next(iter(await repo.get_accounts(["test-token"])))

        self.assertEqual(record.status, AccountStatus.EXPIRED)
        self.assertEqual(record.last_fail_reason, "unauthorized")

    async def test_429_is_persisted_as_rate_limited_cooling(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="test-token")])
            service = AccountRefreshService(repo)
            await service.record_failure_async(
                "test-token", 0, UpstreamError("usage limit", status=429)
            )
            record = next(iter(await repo.get_accounts(["test-token"])))

        self.assertEqual(record.status, AccountStatus.COOLING)
        self.assertEqual(record.last_fail_reason, "rate_limited")
        self.assertEqual(record.state_reason, "rate_limited")
        self.assertGreater(record.ext.get("cooldown_until", 0), 0)
        self.assertEqual(record.ext.get("cooldown_reason"), "rate_limited")

    async def test_legacy_recent_429_is_backfilled_to_cooling(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LocalAccountRepository(Path(tmp) / "accounts.db")
            await repo.initialize()
            await repo.upsert_accounts([AccountUpsert(token="test-token")])
            service = AccountRefreshService(repo)
            with patch("app.control.account.refresh.now_ms", return_value=1_000_000):
                await service.record_failure_async(
                    "test-token", 0, UpstreamError("usage limit", status=429)
                )

            # Simulate the incomplete state persisted by the previous release:
            # quota/failure metadata exists, but lifecycle cooling fields do not.
            await repo.patch_accounts([
                AccountPatch(token="test-token", clear_failures=True),
                AccountPatch(
                    token="test-token",
                    last_fail_at=1_000_000,
                    last_fail_reason="rate_limited",
                ),
            ])

            with patch("app.control.account.refresh.now_ms", return_value=1_001_000):
                repaired = await reconcile_legacy_rate_limited_accounts(repo)
            record = next(iter(await repo.get_accounts(["test-token"])))

        self.assertEqual(repaired, 1)
        self.assertEqual(record.status, AccountStatus.COOLING)
        self.assertEqual(record.state_reason, "rate_limited")
        self.assertGreater(record.ext.get("cooldown_until", 0), 1_001_000)

class RetryConfigurationTests(unittest.TestCase):
    def test_switch_limit_uses_live_config_and_is_clamped(self):
        with patch(
            "app.products._account_selection.get_config", return_value=7
        ):
            self.assertEqual(selection_max_retries(), 7)
        with patch(
            "app.products._account_selection.get_config", return_value=-1
        ):
            self.assertEqual(selection_max_retries(), 0)
        with patch(
            "app.products._account_selection.get_config", return_value=999
        ):
            self.assertEqual(selection_max_retries(), 999)

    def test_default_and_admin_schema_expose_live_switch_limit(self):
        defaults = (_ROOT / "config.defaults.toml").read_text(encoding="utf-8")
        admin_html = (_ROOT / "app/statics/admin/config.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('max_retries = 1', defaults)
        self.assertIn('on_codes = "429,401,403,503"', defaults)
        self.assertIn("key: 'max_retries'", admin_html)
        self.assertIn("section: 'account.error'", admin_html)
        self.assertIn("forbidden_cooling_sec", admin_html)
        schedule_start = admin_html.index("id: 'schedule'")
        schedule_end = admin_html.index("id: 'cache'", schedule_start)
        retry_group = admin_html.index("section: 'retry'")
        self.assertGreater(retry_group, schedule_start)
        self.assertLess(retry_group, schedule_end)


if __name__ == "__main__":
    unittest.main()
