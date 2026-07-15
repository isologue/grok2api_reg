import unittest
from unittest.mock import patch

from app.control.registration.runner import BrowserRegistration, RetryableMailboxError


class _Wait:
    def __init__(self) -> None:
        self.enabled_called = False

    def enabled(self, timeout: int) -> None:
        self.enabled_called = timeout == 5


class _Button:
    def __init__(self) -> None:
        self.wait = _Wait()
        self.clicked = False

    def click(self, timeout: int) -> None:
        self.clicked = timeout == 3


class _Page:
    def __init__(self, button: _Button | None = None) -> None:
        self.button = button

    def ele(self, locator: str, timeout: int = 0):
        return self.button

    def cookies(self, **kwargs):
        return []


class RegistrationProfileSubmitTests(unittest.TestCase):
    @staticmethod
    def _registration() -> BrowserRegistration:
        return object.__new__(BrowserRegistration)

    def test_turnstile_without_checkbox_structure_is_nonfatal(self) -> None:
        registration = self._registration()
        registration.page = _Page(button=None)
        registration._turnstile_state = lambda: "pending"

        self.assertFalse(registration._try_turnstile())

    def test_profile_submit_requires_post_click_confirmation(self) -> None:
        registration = self._registration()
        button = _Button()
        registration.page = _Page(button=button)
        registration._refresh_active_page = lambda: None
        registration._js = lambda *args: True
        registration._turnstile_state = lambda: "ready"
        confirmed = []
        registration._await_profile_submission = lambda: confirmed.append(True)

        profile = registration._submit_profile(timeout=1)

        self.assertTrue(button.wait.enabled_called)
        self.assertTrue(button.clicked)
        self.assertEqual(confirmed, [True])
        self.assertIn("given_name", profile)
        self.assertIn("password", profile)

    def test_profile_submit_tolerates_context_replacement(self) -> None:
        registration = self._registration()
        calls = {"count": 0}
        registration._refresh_active_page = lambda: None
        registration._read_sso_cookie = lambda: None

        def profile_state():
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("ContextLostError: page was refreshed")
            return {"profile_form": False, "errors": []}

        registration._profile_submission_state = profile_state
        with patch("app.control.registration.runner.time.sleep"):
            registration._await_profile_submission(timeout=1)
        self.assertEqual(calls["count"], 2)

    def test_profile_submit_still_on_form_is_retryable(self) -> None:
        registration = self._registration()
        registration._refresh_active_page = lambda: None
        registration._read_sso_cookie = lambda: None
        registration._profile_submission_state = lambda: {
            "profile_form": True,
            "url": "https://accounts.x.ai/sign-up",
            "errors": [],
        }
        registration._sso_diagnostics = lambda: {"profile_form": True}

        with patch("app.control.registration.runner.time.sleep"):
            with self.assertRaisesRegex(RetryableMailboxError, "注册表单"):
                registration._await_profile_submission(timeout=0.001)


if __name__ == "__main__":
    unittest.main()
