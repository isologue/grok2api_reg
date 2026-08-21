from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


_MODULE_PATH = Path(__file__).parents[1] / "app" / "control" / "registration" / "browser_proxy.py"
_SPEC = importlib.util.spec_from_file_location("registration_browser_proxy_under_test", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


class BrowserProxyParsingTests(unittest.TestCase):
    def test_socks5_credentials_are_removed_from_chromium_url(self) -> None:
        proxy = _MODULE.parse_browser_proxy("socks5://user:p%40ss@proxy.example:1080")
        self.assertIsNotNone(proxy)
        assert proxy is not None
        self.assertEqual(proxy.username, "user")
        self.assertEqual(proxy.password, "p@ss")
        self.assertEqual(proxy.chromium_url, "socks5://proxy.example:1080")
        self.assertNotIn("user", proxy.log_label)
        self.assertNotIn("p@ss", proxy.log_label)

    def test_socks5h_is_mapped_to_chromiums_socks5(self) -> None:
        proxy = _MODULE.parse_browser_proxy("socks5h://user:pass@proxy.example:1080")
        self.assertIsNotNone(proxy)
        assert proxy is not None
        self.assertEqual(proxy.chromium_scheme, "socks5")
        self.assertEqual(proxy.chromium_url, "socks5://proxy.example:1080")

    def test_http_auth_proxy_keeps_auth_for_extension_but_not_url(self) -> None:
        proxy = _MODULE.parse_browser_proxy("http://user:pass@proxy.example:3128")
        self.assertIsNotNone(proxy)
        assert proxy is not None
        self.assertTrue(proxy.has_auth)
        self.assertEqual(proxy.chromium_url, "http://proxy.example:3128")

    def test_bare_host_port_defaults_to_http(self) -> None:
        proxy = _MODULE.parse_browser_proxy("proxy.example:8080")
        self.assertIsNotNone(proxy)
        assert proxy is not None
        self.assertEqual(proxy.chromium_url, "http://proxy.example:8080")

    def test_invalid_path_and_unsupported_authenticated_socks4_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _MODULE.parse_browser_proxy("socks5://proxy.example:1080/path")
        proxy = _MODULE.parse_browser_proxy("socks4://user:pass@proxy.example:1080")
        self.assertIsNotNone(proxy)
        assert proxy is not None
        self.assertEqual(proxy.chromium_scheme, "socks4")

    def test_auth_extension_does_not_write_credentials_to_manifest(self) -> None:
        proxy = _MODULE.parse_browser_proxy("http://user:secret@proxy.example:3128")
        assert proxy is not None
        directory = _MODULE.create_proxy_auth_extension(proxy)
        try:
            manifest = (Path(directory) / "manifest.json").read_text(encoding="utf-8")
            background = (Path(directory) / "background.js").read_text(encoding="utf-8")
            self.assertNotIn("secret", manifest)
            self.assertIn("webRequestAuthProvider", manifest)
            self.assertIn("user", background)
        finally:
            _MODULE.remove_proxy_auth_extension(directory)


if __name__ == "__main__":
    unittest.main()
