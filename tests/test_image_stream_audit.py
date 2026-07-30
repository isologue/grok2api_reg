import unittest
from unittest.mock import AsyncMock, patch

import orjson

from app.platform.errors import UpstreamError
from app.products.openai.images import _stream_image_chat_request


class _FakeResponse:
    status_code = 200

    async def aiter_lines(self):
        payload = {
            "result": {
                "response": {
                    "modelResponse": {
                        "streamErrors": [{
                            "message": "You\'ve reached your usage limit. Please try again later.",
                            "usageLimitReached": {},
                        }],
                    },
                },
            },
        }
        yield "data: " + orjson.dumps(payload).decode()


class _FakeProxy:
    async def acquire(self):
        return None


class _FakeSession:
    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, *_args, **_kwargs):
        return _FakeResponse()


class ImageStreamAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_in_band_429_finishes_audit_as_failure_not_transport_200(self):
        trace_failures = []
        with (
            patch("app.products.openai.images.get_proxy_runtime", AsyncMock(return_value=_FakeProxy())),
            patch("app.products.openai.images.build_http_headers", return_value={}),
            patch("app.products.openai.images.build_session_kwargs", return_value={}),
            patch("app.products.openai.images.start_upstream_trace", return_value="trace-1"),
            patch("app.products.openai.images.fail_upstream_trace", side_effect=lambda *args, **kwargs: trace_failures.append((args, kwargs))),
            patch("app.products.openai.images.finish_upstream_trace") as finish_trace,
            patch("app.products.openai.images.ResettableSession", _FakeSession),
        ):
            stream = _stream_image_chat_request(
                token="sso=test-token",
                payload={"message": "test"},
                referer="https://grok.com/",
                timeout_s=1,
                operation="image_generation_lite",
            )
            with self.assertRaises(UpstreamError) as raised:
                async for _ in stream:
                    pass

        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(len(trace_failures), 1)
        self.assertEqual(trace_failures[0][1]["status"], 429)
        finish_trace.assert_not_called()


if __name__ == "__main__":
    unittest.main()
