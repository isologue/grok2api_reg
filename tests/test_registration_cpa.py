from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.registration.cpa import export_cpa_auth
from app.control.registration.cpa_queue import CpaExportQueue
from app.control.registration.manager import RegistrationManager


class CpaSettingsTests(unittest.TestCase):
    def test_cpa_proxy_is_masked_and_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class _Manager(RegistrationManager):
                @property
                def _dir(self) -> Path:
                    path = Path(tmp)
                    path.mkdir(parents=True, exist_ok=True)
                    return path

            manager = _Manager()
            saved = manager.save_settings({
                "cpa": {"enabled": True, "proxy": "http://user:secret@proxy:8080", "mint_gap_sec": 30},
            })
            self.assertTrue(saved["cpa"]["enabled"])
            self.assertEqual(saved["cpa"]["proxy"], "")
            self.assertTrue(saved["cpa"]["proxy_configured"])
            self.assertEqual(manager._read_settings_raw()["cpa"]["proxy"], "http://user:secret@proxy:8080")


class CpaExportTests(unittest.TestCase):
    def test_protocol_first_export_passes_sso_and_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = {"ok": True, "path": str(Path(tmp) / "xai-user.json"), "mint_method": "protocol"}
            with patch("app.control.registration.cpa_xai.mint_and_export", return_value=result) as mint:
                exported = export_cpa_auth(
                    account={"email": "user@example.test", "password": "p", "sso": "sso-value"},
                    cpa={"enabled": True, "auth_dir": tmp, "proxy": "http://warp:8118", "prefer_protocol": True, "probe_after_write": False},
                    cookies=[{"name": "sso", "value": "sso-value"}],
                )

            self.assertTrue(exported["ok"])
            self.assertTrue(mint.call_args.kwargs["prefer_protocol"])
            self.assertEqual(mint.call_args.kwargs["sso"], "sso-value")
            self.assertEqual(mint.call_args.kwargs["proxy"], "http://warp:8118")

    def test_queue_uses_browser_proxy_when_cpa_proxy_is_empty(self) -> None:
        queue = CpaExportQueue({"browser_proxy": "http://warp:8118", "cpa": {"enabled": False}})
        self.assertEqual(queue._cpa["proxy"], "http://warp:8118")


if __name__ == "__main__":
    unittest.main()
