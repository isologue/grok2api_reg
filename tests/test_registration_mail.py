import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.control.registration.mail import MailboxPool, MailboxRateLimited, TempMailLolProvider, extract_verification_code
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

    def test_tempmail_bypasses_browser_proxy_unless_explicitly_enabled(self) -> None:
        with patch("app.control.registration.mail._create_session") as make_session:
            make_session.return_value = Mock(headers={})
            TempMailLolProvider({"type": "tempmail_lol"}, proxy="http://warp-proxy:8118")
            make_session.assert_called_once_with("")

        with patch("app.control.registration.mail._create_session") as make_session:
            make_session.return_value = Mock(headers={})
            TempMailLolProvider({"type": "tempmail_lol", "use_proxy": True}, proxy="http://warp-proxy:8118")
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
                "mail": {"providers": [{"id": "temp", "type": "tempmail_lol", "enabled": True, "domains": ["example.test"], "use_proxy": True}]},
            })
            provider = settings["mail"]["providers"][0]
            self.assertEqual(provider["type"], "tempmail_lol")
            self.assertEqual(provider["domains"], ["example.test"])
            self.assertTrue(provider["use_proxy"])



if __name__ == "__main__":
    unittest.main()
