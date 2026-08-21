from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.control.model_utils import extract_model_ids
from app.control.registration.cpa_queue import CpaExportQueue
from app.control.registration.cpa_xai.mint import mint_and_export


class CpaModelCompatibilityTests(unittest.TestCase):
    def test_extracts_grok_46_from_supported_response_shapes(self) -> None:
        bodies = [
            {"data": [{"id": "grok-4.6"}]},
            {"models": [{"model": "grok-4.6"}]},
            {"data": {"models": [{"name": "grok-4.6"}]}},
            {"result": {"data": [{"id": "grok-4.6"}]}},
        ]
        for body in bodies:
            with self.subTest(body=body):
                self.assertEqual(extract_model_ids(body), ["grok-4.6"])

    def test_model_probe_failure_does_not_fail_written_cpa_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tokens = {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "mint_method": "protocol",
            }
            probe = {"ok": False, "status": 403, "error": "models unavailable", "model_ids": []}
            with (
                patch("app.control.registration.cpa_xai.mint.mint_with_sso_protocol", return_value=tokens),
                patch("app.control.registration.cpa_xai.mint.probe_models", return_value=probe),
            ):
                result = mint_and_export(
                    email="user@example.test",
                    password="password",
                    auth_dir=tmp,
                    sso="sso-cookie",
                    proxy="",
                    probe=True,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["model_ids"], [])
            self.assertTrue(Path(result["path"]).is_file())

    def test_queue_imports_cpa_even_when_probe_has_no_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cpa_file = Path(tmp) / "xai-user.json"
            cpa_file.write_text("{}", encoding="utf-8")
            queue = CpaExportQueue({"cpa": {"enabled": False, "auto_import_build": True}})
            result = {"ok": True, "path": str(cpa_file), "probe_models": {"ok": True, "model_ids": []}}
            with (
                patch("app.control.registration.cpa_queue.export_cpa_auth", return_value=result),
                patch("app.control.registration.cpa_queue.record_cpa_task_result"),
                patch("app.control.build.import_cpa_auth_file", return_value={"added": 1, "updated": 0, "skipped": 0}) as importer,
            ):
                queue._run({"email": "user@example.test", "password": "p", "sso": "s"}, [])
            importer.assert_called_once_with(str(cpa_file), model_ids=[])


if __name__ == "__main__":
    unittest.main()
