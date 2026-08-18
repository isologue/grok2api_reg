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
        self.assertEqual(kwargs["data"], {"file_source": "IMAGINE_SELF_UPLOAD_FILE_SOURCE"})
        self.assertEqual(kwargs["files"]["file"], ("reference.png", b"png-bytes", "image/png"))
        finish_trace.assert_called_once()


if __name__ == "__main__":
    unittest.main()
