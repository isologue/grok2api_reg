import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.control.registration.mail import (
    MailboxPool, MailboxRateLimited, TempMailLolProvider, expand_outlook_aliases,
    extract_verification_code, outlook_pool_stats, parse_outlook_credentials,
    remove_outlook_invalid_credentials, reset_outlook_pool_state,
)
from app.control.registration.manager import RegistrationManager


class TempMailLolProviderTests(unittest.TestCase):
    def test_create_and_read_inbox_with_provider_token(self) -> None:
        provider = TempMailLolProvider({"name": "TempMail.lol", "type": "tempmail_lol", "domains": ["example.test"]})
        provider._request = Mock(return_value={"address": "new@example.test", "token": "inbox-token"})
        created = provider.create_mailbox()
        self.assertEqual(created, {"address": "new@example.test", "token": "inbox-token"})
        self.assertEqual(provider._request.call_args.kwargs["payload"], {"domain": "example.test"})

        provider._request = Mock(return_value={"emails": [{"id": "message-1", "subject": "Your code", "text": "Use ABC-123"}]})
        messages = provider.list_messages("new@example.test", "inbox-token")
        self.assertEqual(messages[0]["id"], "message-1")
        self.assertEqual(provider._request.call_args.kwargs["params"], {"token": "inbox-token"})
        self.assertEqual(provider.message_content("message-1", "inbox-token"), "Use ABC-123")
        self.assertEqual(extract_verification_code("Use ABC-123"), "ABC-123")

    def test_api_key_is_optional_and_attached_when_configured(self) -> None:
        provider = TempMailLolProvider({"type": "tempmail_lol", "api_key": "test-key"})
        self.assertEqual(provider.session.headers["Authorization"], "Bearer test-key")

    def test_tempmail_uses_configured_email_api_proxy(self) -> None:
        with patch("app.control.registration.mail._create_session") as make_session:
            make_session.return_value = Mock(headers={})
            TempMailLolProvider({"type": "tempmail_lol"}, proxy="http://warp-proxy:8118")
            make_session.assert_called_once_with("http://warp-proxy:8118")

    def test_inbox_429_honours_retry_after_and_shares_cooldown(self) -> None:
        provider = TempMailLolProvider({"type": "tempmail_lol"})
        response = Mock(status_code=429, headers={"Retry-After": "21"})
        provider.session.request = Mock(return_value=response)

        with self.assertRaises(MailboxRateLimited) as first:
            provider.list_messages("mail@example.test", "inbox-token")
        self.assertEqual(first.exception.retry_after, 21.0)
        self.assertEqual(provider.session.request.call_count, 1)

        # A second polling attempt must wait locally rather than hit /inbox again.
        with self.assertRaises(MailboxRateLimited):
            provider.list_messages("mail@example.test", "inbox-token")
        self.assertEqual(provider.session.request.call_count, 1)

    def test_inline_tempmail_body_does_not_need_a_second_inbox_request(self) -> None:
        self.assertEqual(
            MailboxPool._inline_message_content({"text": "Use ABC-123", "subject": "Verification"}),
            "Use ABC-123",
        )

    def test_css_identifier_is_not_mistaken_for_a_verification_code(self) -> None:
        content = "<style>.email { TEXT-SIZE: 14px; }</style><p>Your code is ABC-123</p>"
        self.assertEqual(extract_verification_code(content), "ABC-123")
        self.assertIsNone(extract_verification_code("<style>.email { TEXT-SIZE: 14px; }</style>"))


class RegistrationMailboxSettingsTests(unittest.TestCase):
    def test_tempmail_lol_can_be_saved_without_api_key_or_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class _Manager(RegistrationManager):
                @property
                def _dir(self) -> Path:
                    path = Path(tmp)
                    path.mkdir(parents=True, exist_ok=True)
                    return path

            settings = _Manager().save_settings({
                "mail": {"providers": [{"id": "temp", "type": "tempmail_lol", "enabled": True, "domains": ["example.test"]}]},
            })
            provider = settings["mail"]["providers"][0]
            self.assertEqual(provider["type"], "tempmail_lol")
            self.assertEqual(provider["domains"], ["example.test"])
            self.assertNotIn("use_proxy", provider)

    def test_microsoft_alias_options_are_normalized_and_expand_supported_domains(self) -> None:
        credentials = "owner@outlook.com----password----client-id----refresh-token\nexternal@example.test----password----client-id-2----refresh-token-2"
        with tempfile.TemporaryDirectory() as tmp:
            class _Manager(RegistrationManager):
                @property
                def _dir(self) -> Path:
                    path = Path(tmp)
                    path.mkdir(parents=True, exist_ok=True)
                    return path

            saved = _Manager().save_settings({"mail": {"providers": [{"id": "ms", "type": "outlook_token", "enabled": True, "mailboxes": credentials, "mode": "unexpected", "message_limit": 999, "alias_enabled": True, "alias_per_email": 2, "alias_prefix": "tag !", "alias_include_original": False}]}})
            provider = saved["mail"]["providers"][0]
            self.assertEqual(provider["mode"], "auto")
            self.assertEqual(provider["message_limit"], 100)
            self.assertEqual(provider["alias_prefix"], "tag")
            self.assertEqual(provider["mailboxes_base_count"], 2)
            self.assertEqual(provider["mailboxes_count"], 2)

    def test_microsoft_pool_import_merges_by_email_without_returning_secret(self) -> None:
        first = "owner@outlook.com----password----client-a----refresh-a"
        second = "other@hotmail.com----password----client-b----refresh-b"
        with tempfile.TemporaryDirectory() as tmp:
            class _Manager(RegistrationManager):
                @property
                def _dir(self) -> Path:
                    path = Path(tmp)
                    path.mkdir(parents=True, exist_ok=True)
                    return path

            manager = _Manager()
            manager.save_settings({"mail": {"providers": [{"id": "ms", "type": "outlook_token", "enabled": True, "mailboxes": first}]}})
            saved = manager.save_settings({"mail": {"providers": [{"id": "ms", "type": "outlook_token", "enabled": True, "mailboxes": second}]}})
            self.assertEqual(saved["mail"]["providers"][0]["mailboxes_base_count"], 2)
            raw = manager._read_settings_raw()["mail"]["providers"][0]["mailboxes"]
            self.assertIn("refresh-a", raw)
            self.assertIn("refresh-b", raw)


class OutlookPoolStateTests(unittest.TestCase):
    def test_alias_expansion_and_state_maintenance(self) -> None:
        credentials = parse_outlook_credentials("owner@outlook.com----password----client----refresh")
        pool = expand_outlook_aliases(credentials, {"alias_enabled": True, "alias_per_email": 2, "alias_prefix": "c2api", "alias_include_original": True})
        self.assertEqual([item["email"] for item in pool], ["owner@outlook.com", "owner+c2api1@outlook.com", "owner+c2api2@outlook.com"])
        with tempfile.TemporaryDirectory() as tmp, patch("app.control.registration.mail._outlook_state_path", return_value=Path(tmp) / "outlook.json"):
            from app.control.registration import mail
            mail._write_outlook_state({
                "owner@outlook.com": {"state": "used"},
                "owner+c2api1@outlook.com": {"state": "failed"},
                "owner+c2api2@outlook.com": {"state": "token_invalid"},
            })
            stats = outlook_pool_stats(credentials, {"alias_enabled": True, "alias_per_email": 2, "alias_prefix": "c2api", "alias_include_original": True})
            self.assertEqual(stats["used"], 1)
            self.assertEqual(stats["retryable"], 1)
            self.assertEqual(stats["invalid"], 1)
            self.assertEqual(reset_outlook_pool_state("retryable"), 1)
            self.assertEqual(reset_outlook_pool_state("invalid"), 1)
            self.assertEqual(reset_outlook_pool_state("used"), 1)

    def test_invalid_credentials_can_be_removed_without_leaking_or_removing_healthy_rows(self) -> None:
        credentials = parse_outlook_credentials(
            "bad@outlook.com----password----client-a----refresh-a\n"
            "good@hotmail.com----password----client-b----refresh-b"
        )
        with tempfile.TemporaryDirectory() as tmp, patch("app.control.registration.mail._outlook_state_path", return_value=Path(tmp) / "outlook.json"):
            from app.control.registration import mail
            mail._write_outlook_state({"bad@outlook.com": {"state": "token_invalid"}})
            kept, removed = remove_outlook_invalid_credentials(credentials)
            self.assertEqual(removed, 1)
            self.assertEqual([item["email"] for item in kept], ["good@hotmail.com"])

    def test_microsoft_pool_credentials_are_validated_and_masked(self) -> None:
        credentials = "owner@example.test----password----client-id----refresh-token"
        self.assertEqual(parse_outlook_credentials(credentials)[0]["email"], "owner@example.test")
        with tempfile.TemporaryDirectory() as tmp:
            class _Manager(RegistrationManager):
                @property
                def _dir(self) -> Path:
                    path = Path(tmp)
                    path.mkdir(parents=True, exist_ok=True)
                    return path

            manager = _Manager()
            saved = manager.save_settings({"mail": {"providers": [{"id": "ms", "type": "outlook_token", "enabled": True, "mailboxes": credentials}]}})
            provider = saved["mail"]["providers"][0]
            self.assertEqual(provider["mailboxes"], "")
            self.assertTrue(provider["mailboxes_configured"])
            self.assertEqual(provider["mailboxes_count"], 1)
            self.assertIn("refresh-token", manager._read_settings_raw()["mail"]["providers"][0]["mailboxes"])



if __name__ == "__main__":
    unittest.main()
