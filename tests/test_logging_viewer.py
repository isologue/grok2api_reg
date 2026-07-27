import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.platform.logging import request_trace


_ROOT = Path(__file__).resolve().parents[1]
_LOGS_MODULE_SPEC = importlib.util.spec_from_file_location(
    "grok2api_test_logs_module",
    _ROOT / "app/products/web/admin/logs.py",
)
assert _LOGS_MODULE_SPEC is not None and _LOGS_MODULE_SPEC.loader is not None
logs_mod = importlib.util.module_from_spec(_LOGS_MODULE_SPEC)
_LOGS_MODULE_SPEC.loader.exec_module(logs_mod)


class _TraceConfig:
    def get_bool(self, key, default=False):
        return True

    def get_int(self, key, default=0):
        return 1024


class RequestTraceTests(unittest.TestCase):
    def test_sensitive_fields_and_data_urls_are_redacted(self):
        value = request_trace._redact(
            {
                "access_token": "secret-token",
                "password": "password",
                "nested": {"cookie": "session-cookie"},
                "image": "data:image/png;base64,abcdefgh",
                "message": "hello",
                "max_output_tokens": 42,
            }
        )

        self.assertEqual(value["access_token"], "***")
        self.assertEqual(value["password"], "***")
        self.assertEqual(value["nested"]["cookie"], "***")
        self.assertEqual(value["image"], "[data-url:30]")
        self.assertEqual(value["message"], "hello")
        self.assertEqual(value["max_output_tokens"], 42)

    def test_trace_records_mask_the_selected_account(self):
        records = []

        class Logger:
            def info(self, _template, record):
                records.append(record)

        with (
            patch.object(request_trace, "get_config", return_value=_TraceConfig()),
            patch.object(request_trace, "logger", Logger()),
        ):
            trace_id = request_trace.start_upstream_trace(
                account_token="abcdefghijklmnopqrst",
                endpoint="https://example.test/upstream",
                payload={"message": "hello", "token": "never-log-me"},
            )

        self.assertTrue(trace_id)
        self.assertEqual(len(records), 1)
        self.assertIn('"account":"abcdefgh...qrst"', records[0])
        self.assertIn('"token":"***"', records[0])
        self.assertNotIn("never-log-me", records[0])


class AdminLogReaderTests(unittest.TestCase):
    def test_log_reader_lists_and_filters_only_expected_log_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "app_2026-07-27.log").write_text(
                "plain line\nTRACE_UPSTREAM request\n", encoding="utf-8"
            )
            (root / "not-a-log.txt").write_text("ignore", encoding="utf-8")

            with patch.object(logs_mod, "log_dir", return_value=root):
                files = logs_mod._log_files()
                text = logs_mod._read_tail(files[0])
                self.assertEqual([item.name for item in files], ["app_2026-07-27.log"])
                self.assertIn("TRACE_UPSTREAM", text)
                with self.assertRaises(HTTPException):
                    logs_mod._safe_log_file("../secrets.log")


class LogViewerSurfaceTests(unittest.TestCase):
    def test_log_viewer_route_and_ui_are_present(self):
        router = (_ROOT / "app/products/web/router.py").read_text(encoding="utf-8")
        header = (_ROOT / "app/statics/admin/header.html").read_text(encoding="utf-8")
        page = (_ROOT / "app/statics/admin/logs.html").read_text(encoding="utf-8")
        defaults = (_ROOT / "config.defaults.toml").read_text(encoding="utf-8")

        self.assertIn('"/admin/logs"', router)
        self.assertIn('href="/admin/logs"', header)
        self.assertIn("TRACE_UPSTREAM", page)
        self.assertIn("[logging.trace]", defaults)
        self.assertIn("max_chars = 16000", defaults)


if __name__ == "__main__":
    unittest.main()
