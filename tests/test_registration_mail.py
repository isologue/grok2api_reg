import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.control.registration.mail import (
    CloudflareTempMailProvider, MailboxPool, MailboxRateLimited, OutlookTokenError, OutlookTokenProvider, RemailProvider, TempMailLolProvider, expand_outlook_aliases,
    extract_verification_code, outlook_pool_stats, parse_outlook_credentials,
    remove_outlook_invalid_credentials, reset_outlook_pool_state,
)
from app.control.registration.manager import RegistrationManager


class CloudflareTempMailProviderTests(unittest.TestCase):
    def test_create_and_read_inbox_with_jwt(self) -> None:
        provider = CloudflareTempMailProvider({
            "name": "Cloudflare Temp Email",
            "type": "cloudflare_temp_email",
            "api_base": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domains": ["example.test"],
        })
        provider._request = Mock(side_effect=[
            {"address": "new@example.test", "jwt": "mailbox-jwt"},
            {"results": [{
                "id": "message-1",
                "subject": "Verify your email",
                "text": "Your code is ABC-123",
                "createdAt": "2026-08-11T01:02:03Z",
            }]},
        ])
        created = provider.create_mailbox()
        self.assertEqual(created, {"address": "new@example.test", "token": "mailbox-jwt"})
        self.assertEqual(provider._request.call_args_list[0].kwargs["payload"]["domain"], "example.test")
        messages = provider.list_messages(created["address"], created["token"])
        self.assertEqual(messages[0]["id"], "message-1")
        self.assertIn("ABC-123", messages[0]["content"])
        self.assertEqual(provider.message_content("message-1", created["token"]), "Your code is ABC-123")
        provider.close()

    def test_admin_password_is_sent_only_to_admin_endpoints(self) -> None:
        provider = CloudflareTempMailProvider({
            "type": "cloudflare_temp_email",
            "api_base": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domains": ["example.test"],
        })
        response = Mock(status_code=200)
        response.json.return_value = {"address": "new@example.test", "jwt": "mailbox-jwt"}
        provider.session.request = Mock(return_value=response)
        provider.create_mailbox()
        headers = provider.session.request.call_args.kwargs["headers"]
        self.assertEqual(headers["x-admin-auth"], "admin-secret")
        response.json.return_value = {"results": []}
        provider.list_messages("new@example.test", "mailbox-jwt")
        inbox_headers = provider.session.request.call_args.kwargs["headers"]
        self.assertNotIn("x-admin-auth", inbox_headers)
        self.assertEqual(inbox_headers["Authorization"], "Bearer mailbox-jwt")
        provider.close()

    def test_shared_inbox_results_are_filtered_when_recipient_is_present(self) -> None:
        provider = CloudflareTempMailProvider({
            "type": "cloudflare_temp_email",
            "api_base": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domains": ["example.test"],
        })
        provider._request = Mock(return_value={"results": [
            {"id": "wrong", "to": "other@example.test", "text": "code ABC-111"},
            {"id": "right", "to": [{"address": "new@example.test"}], "text": "code ABC-222"},
            {"id": "legacy", "text": "code ABC-333"},
        ]})
        messages = provider.list_messages("new@example.test", "mailbox-jwt")
        self.assertEqual([item["id"] for item in messages], ["right", "legacy"])
        provider.close()

    def test_nested_message_list_and_raw_mime_content_are_supported(self) -> None:
        provider = CloudflareTempMailProvider({
            "type": "cloudflare_temp_email",
            "api_base": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domains": ["example.test"],
        })
        provider._request = Mock(return_value={"data": {"messages": [{
            "msgid": "message-from-msgid",
            # ``address`` may be a sender-like value in deployed CF services;
            # it must not reject the mailbox-scoped JWT result.
            "address": "noreply@accounts.x.ai",
            "raw": "From: xAI <noreply@accounts.x.ai>\nTo: new@example.test\nSubject: Verify\nContent-Type: text/plain; charset=utf-8\n\nYour code is 123456",
        }]}})
        messages = provider.list_messages("new@example.test", "mailbox-jwt")
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "message-from-msgid")
        self.assertEqual(extract_verification_code(messages[0]["content"]), "123456")
        provider.close()

    def test_hydra_member_and_message_id_are_supported(self) -> None:
        provider = CloudflareTempMailProvider({
            "type": "cloudflare_temp_email",
            "api_base": "https://mail.example.test",
            "admin_password": "admin-secret",
            "domains": ["example.test"],
        })
        provider._request = Mock(return_value={"hydra:member": [{
            "messageId": "message-id-camelcase", "html": "<p>Use ABC-123</p>",
        }]})
        messages = provider.list_messages("new@example.test", "mailbox-jwt")
        self.assertEqual(messages[0]["id"], "message-id-camelcase")
        self.assertEqual(extract_verification_code(messages[0]["content"]), "ABC-123")
        provider.close()


class RemailProviderTests(unittest.TestCase):
    def test_create_code_order_and_poll_verification_code(self) -> None:
        provider = RemailProvider({
            "name": "Remail",
            "type": "remail",
            "api_base": "https://remail.example.test",
            "api_key": "rk-test-key",
            "project_id": 42,
            "email_suffix": "outlook.com",
            "supply": "private_first",
        })
        provider._request = Mock(side_effect=[
            {"orderNo": "R20260826001", "deliveryEmail": "person@outlook.com", "status": "active"},
            {"orderNo": "R20260826001", "status": "active", "verificationCode": "L3K-YY9", "lastMailReceivedAt": "2026-08-26T12:00:00Z"},
        ])

        created = provider.create_mailbox()
        self.assertEqual(created["address"], "person@outlook.com")
        self.assertEqual(json.loads(created["token"]), {"order_no": "R20260826001"})
        create_call = provider._request.call_args_list[0]
        self.assertEqual(create_call.args[:2], ("POST", "/v1/open/orders"))
        self.assertEqual(create_call.kwargs["params"], {"serviceMode": "code", "supply": "private_first"})
        self.assertEqual(create_call.kwargs["payload"], {"projectId": 42, "emailSuffix": "outlook.com"})
        self.assertIn("Idempotency-Key", create_call.kwargs["headers"])

        messages = provider.list_messages(created["address"], created["token"])
        self.assertEqual(len(messages), 1)
        self.assertIn("L3K-YY9", messages[0]["content"])
        self.assertEqual(provider._request.call_args_list[1].args[:2], ("GET", "/v1/open/orders/R20260826001"))
        provider.close()

    def test_order_without_code_is_not_treated_as_a_historical_message(self) -> None:
        provider = RemailProvider({
            "type": "remail", "api_key": "rk-test-key", "project_id": 42, "email_suffix": "outlook.com",
        })
        provider._request = Mock(return_value={"orderNo": "R20260826002", "status": "active", "verificationCode": ""})
        self.assertEqual(provider.list_messages("person@outlook.com", json.dumps({"order_no": "R20260826002"})), [])
        provider.close()

    def test_list_projects_keeps_only_code_enabled_suffixes(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "items": [
                {"id": 9, "name": "Ignored", "products": [{"status": "disabled", "codeEnabled": True, "suffixes": [{"suffix": "ignored.example", "totalAvailable": 99}]}]},
                {"id": 3, "name": "Public code", "products": [
                    {"status": "enabled", "codeEnabled": False, "suffixes": [{"suffix": "no-code.example", "totalAvailable": 88}]},
                    {"status": "enabled", "codeEnabled": True, "suffixes": [
                        {"suffix": "Outlook.com", "totalAvailable": 7, "publicAvailable": 4},
                        {"suffix": "hotmail.com", "totalAvailable": "2", "publicAvailable": "2"},
                    ]},
                ]},
                {"id": 4, "name": "Private code", "products": [{"status": "enabled", "codeEnabled": True, "suffixes": [{"suffix": "outlook.com", "totalAvailable": 11, "publicAvailable": 0}]}]},
                {"id": 5, "name": "No suffix", "products": [{"status": "enabled", "codeEnabled": True, "suffixes": []}]},
            ]
        }
        session = Mock()
        session.get.return_value = response
        with patch("app.control.registration.mail._create_session", return_value=session) as create_session:
            projects = RemailProvider.list_projects("https://remail.example.test/", "rk-test-key", "http://proxy:8080")

        self.assertEqual([item["id"] for item in projects], [4, 3])
        self.assertEqual(projects[0]["suffixes"], [{"suffix": "outlook.com", "total_available": 11, "public_available": 0}])
        self.assertEqual(projects[1]["suffixes"], [
            {"suffix": "outlook.com", "total_available": 7, "public_available": 4},
            {"suffix": "hotmail.com", "total_available": 2, "public_available": 2},
        ])
        create_session.assert_called_once_with("http://proxy:8080")
        self.assertEqual(session.get.call_args.args[0], "https://remail.example.test/v1/projects")
        self.assertEqual(session.get.call_args.kwargs["params"], {"scope": "visible", "status": "listed", "offset": 0, "limit": 100})
        self.assertEqual(session.get.call_args.kwargs["headers"]["Authorization"], "Bearer rk-test-key")
        session.close.assert_called_once()

    def test_list_projects_reads_all_offset_pages(self) -> None:
        def project(project_id: int) -> dict:
            return {
                "id": project_id,
                "name": f"Project {project_id}",
                "products": [{"status": "enabled", "codeEnabled": True, "suffixes": [{"suffix": "outlook.com", "totalAvailable": project_id, "publicAvailable": project_id}]}],
            }

        first = Mock(status_code=200)
        first.json.return_value = {"items": [project(index) for index in range(1, 101)], "total": 101, "offset": 0, "limit": 100}
        second = Mock(status_code=200)
        second.json.return_value = {"items": [project(101)], "total": 101, "offset": 100, "limit": 100}
        session = Mock()
        session.get.side_effect = [first, second]
        with patch("app.control.registration.mail._create_session", return_value=session):
            projects = RemailProvider.list_projects("https://remail.example.test", "rk-test-key")

        self.assertEqual(len(projects), 101)
        self.assertEqual([call.kwargs["params"]["offset"] for call in session.get.call_args_list], [0, 100])
        session.close.assert_called_once()


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

    def test_xai_hyphenated_code_is_found_when_html_text_is_concatenated(self) -> None:
        content = (
            "Validate your email Hi, Thank you for creating a SpaceXAI account. "
            "Please use the code below to validate your email address. "
            "L3K-YY9If you did not create a new account, please ignore this email."
        )
        self.assertEqual(extract_verification_code(content), "L3K-YY9")


class OutlookCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _response(status_code: int, payload: dict[str, object]) -> Mock:
        response = Mock(status_code=status_code)
        response.json.return_value = payload
        return response

    def test_refresh_tries_consumers_without_scope_before_common(self) -> None:
        provider = OutlookTokenProvider({"type": "outlook_token", "mailboxes": "owner@hotmail.com----password----client----refresh"})
        provider._session.post = Mock(side_effect=[
            self._response(400, {"error": "invalid_grant", "error_description": "AADSTS70000"}),
            self._response(200, {"access_token": "access-token"}),
        ])
        credential = {"email": "owner@hotmail.com", "client_id": "client", "refresh_token": "refresh", "login_email": "owner@hotmail.com"}

        self.assertEqual(provider._access_token(credential, "offline_access https://graph.microsoft.com/Mail.Read"), "access-token")
        calls = provider._session.post.call_args_list
        self.assertEqual(calls[0].args[0], "https://login.microsoftonline.com/consumers/oauth2/v2.0/token")
        self.assertEqual(calls[1].args[0], "https://login.microsoftonline.com/common/oauth2/v2.0/token")
        self.assertNotIn("scope", calls[0].kwargs["data"])
        self.assertNotIn("scope", calls[1].kwargs["data"])
        provider.close()

    def test_auto_mode_uses_outlook_rest_after_graph_compatibility_failure(self) -> None:
        provider = OutlookTokenProvider({"type": "outlook_token", "mailboxes": "owner@hotmail.com----password----client----refresh", "mode": "auto"})
        provider._graph_messages = Mock(side_effect=RuntimeError("Graph does not accept legacy token"))
        provider._outlook_rest_messages = Mock(return_value=[{"id": "legacy-message", "subject": "code", "content": "ABC-123"}])
        token = '{"email":"owner@hotmail.com","client_id":"client","refresh_token":"refresh","login_email":"owner@hotmail.com"}'

        messages = provider.list_messages("owner@hotmail.com", token)
        self.assertEqual(messages[0]["id"], "legacy-message")
        provider._outlook_rest_messages.assert_called_once()
        provider.close()

    def test_outlook_rest_message_is_normalised(self) -> None:
        provider = OutlookTokenProvider({"type": "outlook_token", "mailboxes": "owner@hotmail.com----password----client----refresh", "mode": "auto"})
        provider._access_token = Mock(return_value="legacy-access-token")
        provider._session.get = Mock(return_value=self._response(200, {"value": [{"Id": "m1", "Subject": "Verify", "Body": {"Content": "Use ABC-123"}}]}))
        credential = {"email": "owner@hotmail.com", "client_id": "client", "refresh_token": "refresh", "login_email": "owner@hotmail.com"}

        self.assertEqual(provider._outlook_rest_messages(credential), [{"id": "m1", "subject": "Verify", "content": "Use ABC-123"}])
        self.assertEqual(provider._session.get.call_args.args[0], "https://outlook.office.com/api/v2.0/me/messages")
        provider.close()


class MailboxBaselineTests(unittest.TestCase):
    class _Provider:
        poll_interval_seconds = 0.0

        def __init__(self) -> None:
            self.messages = [{"id": "old", "subject": "Old code", "content": "Use OLD-111"}]

        def list_messages(self, address: str, provider_token: str):
            return list(self.messages)

        def message_content(self, message_id: str, provider_token: str) -> str:
            return ""

    def test_pre_send_baseline_rejects_historical_codes_from_shared_inbox(self) -> None:
        provider = self._Provider()
        pool = object.__new__(MailboxPool)
        pool._providers = [provider]
        token = '{"provider_index":0,"provider_token":"context"}'
        baseline = pool.capture_inbox_baseline("alias@example.test", token)
        provider.messages.insert(0, {"id": "new", "subject": "New code", "content": "Use NEW-222"})
        polls = iter([True, False])
        pool._sleep_until_next_poll = lambda deadline, seconds: next(polls)

        self.assertEqual(pool.wait_for_code("alias@example.test", token, timeout=1, known_message_ids=baseline), "NEW-222")


class OutlookPreflightTests(unittest.TestCase):
    def test_preflight_marks_only_definitive_token_failures_invalid(self) -> None:
        entry = {"type": "outlook_token", "mailboxes": "owner@outlook.com----password----client----refresh", "alias_enabled": True, "alias_per_email": 1, "preflight_enabled": True}
        provider = OutlookTokenProvider(entry)
        provider.list_messages = Mock(side_effect=OutlookTokenError("Microsoft token refresh failed: HTTP 400 (invalid_grant AADSTS70000)"))
        with tempfile.TemporaryDirectory() as tmp, patch("app.control.registration.mail._outlook_state_path", return_value=Path(tmp) / "outlook.json"):
            outcome = provider.preflight()
            from app.control.registration import mail
            state = mail._read_outlook_state()
            self.assertEqual(outcome, {"checked": 1, "available": 0, "invalid": 1, "transient": 0})
            self.assertEqual(state["owner@outlook.com"]["state"], "token_invalid")
            self.assertEqual(state["owner+c2api1@outlook.com"]["state"], "token_invalid")
        provider.close()

    def test_runtime_refresh_failure_quarantines_every_alias_of_the_credential(self) -> None:
        entry = {
            "type": "outlook_token",
            "mailboxes": "owner@outlook.com----password----client----refresh",
            "alias_enabled": True,
            "alias_per_email": 2,
            "alias_prefix": "c2api",
            "alias_include_original": True,
        }
        provider = OutlookTokenProvider(entry)
        with tempfile.TemporaryDirectory() as tmp, patch("app.control.registration.mail._outlook_state_path", return_value=Path(tmp) / "outlook.json"):
            created = provider.create_mailbox()
            provider.mark_result(
                created["address"],
                created["token"],
                success=False,
                reason="Microsoft token refresh failed: HTTP 400 (invalid_grant AADSTS70000)",
            )
            from app.control.registration import mail
            state = mail._read_outlook_state()
            self.assertEqual(
                {item["state"] for item in state.values()},
                {"token_invalid"},
            )
            self.assertEqual(
                set(state),
                {"owner@outlook.com", "owner+c2api1@outlook.com", "owner+c2api2@outlook.com"},
            )
            with self.assertRaisesRegex(RuntimeError, "no available mailbox"):
                provider.create_mailbox()
        provider.close()

    def test_preflight_skips_a_credential_already_marked_invalid(self) -> None:
        entry = {
            "type": "outlook_token",
            "mailboxes": "owner@outlook.com----password----client----refresh",
            "alias_enabled": True,
            "alias_per_email": 1,
            "preflight_enabled": True,
        }
        provider = OutlookTokenProvider(entry)
        provider.list_messages = Mock()
        with tempfile.TemporaryDirectory() as tmp, patch("app.control.registration.mail._outlook_state_path", return_value=Path(tmp) / "outlook.json"):
            from app.control.registration import mail
            mail._write_outlook_state({
                "owner@outlook.com": {"state": "token_invalid"},
                "owner+c2api1@outlook.com": {"state": "token_invalid"},
            })
            outcome = provider.preflight()
            self.assertEqual(outcome, {"checked": 1, "available": 0, "invalid": 1, "transient": 0})
            provider.list_messages.assert_not_called()
        provider.close()

    def test_preflight_defers_transient_oauth_failures_without_marking_invalid(self) -> None:
        entry = {"type": "outlook_token", "mailboxes": "owner@outlook.com----password----client----refresh", "preflight_enabled": True}
        provider = OutlookTokenProvider(entry)
        provider.list_messages = Mock(side_effect=OutlookTokenError("Microsoft token refresh failed: consumers: ProxyError", definitive=False))
        with tempfile.TemporaryDirectory() as tmp, patch("app.control.registration.mail._outlook_state_path", return_value=Path(tmp) / "outlook.json"):
            outcome = provider.preflight()
            from app.control.registration import mail
            self.assertEqual(outcome, {"checked": 1, "available": 0, "invalid": 0, "transient": 1})
            self.assertEqual(mail._read_outlook_state(), {})
        provider.close()

    def test_preflight_can_be_disabled_for_a_provider(self) -> None:
        provider = OutlookTokenProvider({"type": "outlook_token", "mailboxes": "owner@outlook.com----password----client----refresh", "preflight_enabled": False})
        provider.list_messages = Mock()
        self.assertEqual(provider.preflight(), {"checked": 0, "available": 0, "invalid": 0, "transient": 0})
        provider.list_messages.assert_not_called()
        provider.close()


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
            self.assertEqual(provider["api_base"], "https://api.tempmail.lol/v2")
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

    def test_remail_settings_keep_api_key_masked_and_preserve_order_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class _Manager(RegistrationManager):
                @property
                def _dir(self) -> Path:
                    path = Path(tmp)
                    path.mkdir(parents=True, exist_ok=True)
                    return path

            manager = _Manager()
            saved = manager.save_settings({"mail": {"providers": [{
                "id": "remail", "type": "remail", "enabled": True,
                "api_key": "rk-secret", "project_id": "42", "email_suffix": "Outlook.com", "supply": "public_only",
            }]}})
            provider = saved["mail"]["providers"][0]
            self.assertEqual(provider["api_base"], "https://remail.aishop6.com")
            self.assertEqual(provider["api_key"], "")
            self.assertTrue(provider["api_key_configured"])
            self.assertEqual(provider["project_id"], 42)
            self.assertEqual(provider["email_suffix"], "outlook.com")
            self.assertEqual(provider["supply"], "public_only")
            self.assertEqual(manager._read_settings_raw()["mail"]["providers"][0]["api_key"], "rk-secret")

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
