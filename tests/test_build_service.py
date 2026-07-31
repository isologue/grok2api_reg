from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.platform.errors import UpstreamError
from app.products.build import service


class _Config:
    def __init__(self, retries: int) -> None:
        self.retries = retries

    def get_int(self, key: str, default: int = 0) -> int:
        self.last_key = key
        return self.retries if key == "build.max_retries" else default


class BuildServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_build_retry_count_has_no_code_side_cap(self) -> None:
        with patch("app.products.build.service.get_config", return_value=_Config(999)):
            self.assertEqual(service._retry_count(), 999)

    def test_build_retry_count_clamps_only_negative_values(self) -> None:
        with patch("app.products.build.service.get_config", return_value=_Config(-2)):
            self.assertEqual(service._retry_count(), 0)

    async def test_failed_refresh_is_not_mistaken_for_a_second_auth_replay(self) -> None:
        error = UpstreamError("Grok Build OAuth refresh failed", status=401)
        error.details["build_oauth_refresh"] = True
        with (
            patch("app.products.build.service.create_response", AsyncMock(side_effect=error)),
            patch("app.products.build.service.refresh_account", AsyncMock()) as refresh,
        ):
            with self.assertRaises(UpstreamError):
                await service._create_with_auth_replay(object(), {"model": "grok-4.5"})
        refresh.assert_not_awaited()

    async def test_stream_second_401_after_refresh_marks_account_rejected(self) -> None:
        calls = 0
        async def fake_stream(_account, _payload):
            nonlocal calls
            calls += 1
            if False:
                yield ""
            raise UpstreamError("Grok Build response failed", status=401)

        refreshed = object()
        with patch("app.products.build.service.stream_response", fake_stream), patch("app.products.build.service.refresh_account", AsyncMock(return_value=refreshed)) as refresh:
            stream = service._stream_with_auth_replay(object(), {"model": "grok-4.5"})
            with self.assertRaises(UpstreamError) as ctx:
                async for _line in stream:
                    pass
        self.assertEqual(calls, 2)
        refresh.assert_awaited_once()
        self.assertTrue(ctx.exception.details.get("build_auth_rechecked"))



if __name__ == "__main__":
    unittest.main()
