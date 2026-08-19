import unittest
from unittest.mock import AsyncMock, patch

import orjson

from app.dataplane.reverse.transport.asset_upload import _upload_imagine_image_inner


class _Config:
    def get_float(self, _key: str, default: float) -> float:
        return default


class _Proxy:
    def __init__(self) -> None:
        self.feedback = AsyncMock()

    async def acquire(self):
        return None


class _Response:
    status_code = 200
    content = orjson.dumps({
        "uploadId": "upload-1",
        "fileMetadata": {
            "fileMetadataId": "asset-123",
            "fileUri": "users/example/asset-123/content",
        },
    })


class _Session:
    post_args = None

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, *args, **kwargs):
        type(self).post_args = (args, kwargs)
        return _Response()




class _RegionResponse:
    status_code = 403
    content = b"<!doctype html><p>This service is not available in your region.</p>"


class _LegacyResponse:
    status_code = 200
    content = orjson.dumps({
        "fileMetadataId": "legacy-asset-456",
        "fileUri": "users/example/legacy-asset-456/content",
    })


class _SequenceSession:
    responses = [_RegionResponse(), _LegacyResponse()]
    calls = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def post(self, *args, **kwargs):
        type(self).calls.append((args, kwargs))
        return type(self).responses.pop(0)

class ImageEditUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_imagine_upload_uses_current_multipart_endpoint_and_metadata_id(self) -> None:
        proxy = _Proxy()
        with (
            patch("app.dataplane.reverse.transport.asset_upload.get_config", return_value=_Config()),
            patch("app.dataplane.reverse.transport.asset_upload.get_proxy_runtime", AsyncMock(return_value=proxy)),
            patch("app.dataplane.reverse.transport.asset_upload.build_http_headers", return_value={"Content-Type": "application/json"}),
            patch("app.dataplane.reverse.transport.asset_upload.build_session_kwargs", return_value={}),
            patch("app.dataplane.reverse.transport.asset_upload.ResettableSession", _Session),
            patch("app.dataplane.reverse.transport.asset_upload.start_upstream_trace", return_value="trace-1"),
            patch("app.dataplane.reverse.transport.asset_upload.finish_upstream_trace") as finish_trace,
        ):
            file_id, file_uri = await _upload_imagine_image_inner(
                "sso=test",
                "reference.png",
                "image/png",
                b"png-bytes",
            )

        self.assertEqual((file_id, file_uri), ("asset-123", "users/example/asset-123/content"))
        args, kwargs = _Session.post_args
        self.assertEqual(args[0], "https://grok.com/http/upload-file-v2/direct")
        self.assertNotIn("Content-Type", kwargs["headers"])
        multipart = kwargs["multipart"]
        self.assertEqual(type(multipart).__name__, "CurlMime")
        finish_trace.assert_called_once()


    async def test_region_403_falls_back_to_legacy_upload_on_same_proxy(self) -> None:
        proxy = _Proxy()
        _SequenceSession.responses = [_RegionResponse(), _LegacyResponse()]
        _SequenceSession.calls = []
        with (
            patch("app.dataplane.reverse.transport.asset_upload.get_config", return_value=_Config()),
            patch("app.dataplane.reverse.transport.asset_upload.get_proxy_runtime", AsyncMock(return_value=proxy)),
            patch("app.dataplane.reverse.transport.asset_upload.build_http_headers", return_value={"Content-Type": "application/json"}),
            patch("app.dataplane.reverse.transport.asset_upload.build_session_kwargs", return_value={}),
            patch("app.dataplane.reverse.transport.asset_upload.ResettableSession", _SequenceSession),
            patch("app.dataplane.reverse.transport.asset_upload.start_upstream_trace", side_effect=["direct", "legacy"]),
            patch("app.dataplane.reverse.transport.asset_upload.fail_upstream_trace") as fail_trace,
            patch("app.dataplane.reverse.transport.asset_upload.logger.warning") as warning,
        ):
            file_id, file_uri = await _upload_imagine_image_inner(
                "sso=test",
                "reference.png",
                "image/png",
                b"png-bytes",
            )

        self.assertEqual((file_id, file_uri), ("legacy-asset-456", "users/example/legacy-asset-456/content"))
        self.assertEqual(
            [call[0][0] for call in _SequenceSession.calls],
            [
                "https://grok.com/http/upload-file-v2/direct",
                "https://grok.com/rest/app-chat/upload-file",
            ],
        )
        self.assertEqual(fail_trace.call_count, 1)
        warning.assert_called_once()

    async def test_non_region_403_does_not_fall_back(self) -> None:
        class _ForbiddenResponse:
            status_code = 403
            content = b"forbidden: invalid session"

        class _ForbiddenSession(_SequenceSession):
            responses = [_ForbiddenResponse()]
            calls = []

        proxy = _Proxy()
        with (
            patch("app.dataplane.reverse.transport.asset_upload.get_config", return_value=_Config()),
            patch("app.dataplane.reverse.transport.asset_upload.get_proxy_runtime", AsyncMock(return_value=proxy)),
            patch("app.dataplane.reverse.transport.asset_upload.build_http_headers", return_value={"Content-Type": "application/json"}),
            patch("app.dataplane.reverse.transport.asset_upload.build_session_kwargs", return_value={}),
            patch("app.dataplane.reverse.transport.asset_upload.ResettableSession", _ForbiddenSession),
            patch("app.dataplane.reverse.transport.asset_upload.start_upstream_trace", return_value="direct"),
            patch("app.dataplane.reverse.transport.asset_upload.fail_upstream_trace"),
        ):
            with self.assertRaisesRegex(Exception, "Imagine reference upload returned 403"):
                await _upload_imagine_image_inner(
                    "sso=test",
                    "reference.png",
                    "image/png",
                    b"png-bytes",
                )

        self.assertEqual(len(_ForbiddenSession.calls), 1)


if __name__ == "__main__":
    unittest.main()
