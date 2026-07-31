import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.platform import request_audit
from app.platform.logging import cleanup


class PersistentLogCleanupTests(unittest.TestCase):
    def test_request_audit_retention_removes_only_dated_old_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Path(tmp) / "request_audits.jsonl"
            journal.write_bytes(
                b"{\"id\":\"old\",\"created_at\":100}\n"
                b"{\"id\":\"new\",\"created_at\":200}\n"
                b"{\"id\":\"legacy\"}\n"
                b"not-valid-json\n"
            )
            with patch.object(request_audit, "_PATH", journal):
                self.assertEqual(request_audit.purge_before(150), 1)

            rows = journal.read_bytes().splitlines()
            self.assertIn(b'{"id":"new","created_at":200}', rows)
            self.assertIn(b'{"id":"legacy"}', rows)
            self.assertIn(b"not-valid-json", rows)
            self.assertNotIn(b'{"id":"old","created_at":100}', rows)

    def test_cpa_task_log_retention_preserves_active_and_new_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            registration_logs = Path(tmp) / "registration"
            registration_logs.mkdir()
            old = registration_logs / "old-task.log"
            active = registration_logs / "active-task.log"
            fresh = registration_logs / "fresh-task.log"
            ignored = registration_logs / "cpa-auth.json"
            for path in (old, active, fresh, ignored):
                path.write_text("log", encoding="utf-8")
            now = 1_000_000.0
            old_time = now - 8 * 86_400
            os.utime(old, (old_time, old_time))
            os.utime(active, (old_time, old_time))
            os.utime(ignored, (old_time, old_time))

            with patch.object(cleanup, "log_path", return_value=registration_logs):
                removed = cleanup.purge_cpa_task_logs_once(
                    7,
                    current_time=now,
                    active_task_ids={"active-task"},
                )

            self.assertEqual(removed, 1)
            self.assertFalse(old.exists())
            self.assertTrue(active.exists())
            self.assertTrue(fresh.exists())
            self.assertTrue(ignored.exists())

    def test_zero_retention_disables_cpa_task_log_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            registration_logs = Path(tmp) / "registration"
            registration_logs.mkdir()
            old = registration_logs / "old-task.log"
            old.write_text("log", encoding="utf-8")
            old_time = 1_000_000.0 - 99 * 86_400
            os.utime(old, (old_time, old_time))

            with patch.object(cleanup, "log_path", return_value=registration_logs):
                self.assertEqual(
                    cleanup.purge_cpa_task_logs_once(0, current_time=1_000_000.0),
                    0,
                )
            self.assertTrue(old.exists())

    def test_retention_defaults_and_configuration_page_are_exposed(self):
        root = Path(os.environ.get("GROK2API_ROOT", Path(__file__).resolve().parents[1]))
        defaults = root / "config.defaults.toml"
        config_page = root / "app" / "statics" / "admin" / "config.html"
        default_text = defaults.read_text(encoding="utf-8")
        config_text = config_page.read_text(encoding="utf-8")

        self.assertIn("request_audit_retention_days = 7", default_text)
        self.assertIn("cpa_task_retention_days = 7", default_text)
        self.assertIn("key: 'request_audit_retention_days'", config_text)
        self.assertIn("key: 'cpa_task_retention_days'", config_text)


if __name__ == "__main__":
    unittest.main()
