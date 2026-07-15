import os
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

from app.control.registration.archive import ARCHIVE_EXT_KEY, build_export_record, decrypt_profile, encrypt_profile


class RegistrationArchiveTests(unittest.TestCase):
    def test_profile_is_encrypted_and_exports_in_grok_build_shape(self) -> None:
        profile = {
            "provider": "grok_build",
            "email": "user@example.test",
            "password": "private-password",
            "oauth": {
                "client_id": "client-1",
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "id_token": "id-secret",
                "expires_at": "2026-07-15T01:00:00Z",
                "user_id": "user-1",
            },
        }
        with patch.dict(os.environ, {"REGISTRATION_ARCHIVE_KEY": Fernet.generate_key().decode("ascii")}, clear=False):
            encrypted = encrypt_profile(profile)
            self.assertNotIn("access-secret", str(encrypted))
            self.assertEqual(decrypt_profile({ARCHIVE_EXT_KEY: encrypted}), profile)
            exported = build_export_record("sso-secret", {ARCHIVE_EXT_KEY: encrypted})
            self.assertEqual(exported["email"], "user@example.test")
            self.assertEqual(exported["refresh_token"], "refresh-secret")
            self.assertNotIn("password", exported)
            with_password = build_export_record("sso-secret", {ARCHIVE_EXT_KEY: encrypted}, include_password=True)
            self.assertEqual(with_password["password"], "private-password")


if __name__ == "__main__":
    unittest.main()
