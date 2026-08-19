"""Asset upload transport — direct base64 upload to Grok.

Calls POST /rest/app-chat/upload-file with base64-encoded content and
returns the file metadata ID used as a file attachment reference in chat.
"""

import asyncio
import base64
import binascii
import mimetypes
import re
from urllib.parse import urlparse

import orjson

from app.platform.logging.logger import logger
from app.platform.logging.request_trace import (
    fail_upstream_trace,
    finish_upstream_trace,
    start_upstream_trace,
)
from app.platform.config.snapshot import get_config
from app.platform.errors import UpstreamError, ValidationError
from app.dataplane.proxy import get_proxy_runtime
from app.dataplane.proxy.adapters.headers import build_sso_cookie
from app.dataplane.proxy.adapters.headers import build_http_headers
from app.dataplane.proxy.adapters.session import ResettableSession, build_session_kwargs
from app.dataplane.reverse.protocol.xai_assets import resolve_asset_reference
from app.control.proxy.feedback import build_feedback
from app.control.proxy.models import ProxyFeedback, ProxyFeedbackKind

_UPLOAD_URL = "https://grok.com/rest/app-chat/upload-file"
# The current Imagine UI uploads reference images as multipart binary data and
# passes fileMetadataId through mediaGenInput.imageToImage.inputAssets.
# Keep the legacy endpoint below for chat attachments and video references.
_IMAGINE_DIRECT_UPLOAD_URL = "https://grok.com/http/upload-file-v2/direct"
_IMAGINE_FILE_SOURCE = "IMAGINE_SELF_UPLOAD_FILE_SOURCE"
_X_USER_ID_RE = re.compile(r"(?:^|;\s*)x-userid=([^;]+)")
_REGION_RESTRICTION_MARKER = "not available in your region"


def _is_imagine_region_restricted(status_code: int, body_text: str) -> bool:
    """Return whether Imagine rejected the configured proxy exit region.

    A 403 alone is ambiguous: it can mean an expired session, a challenge, or
    a policy rejection.  Only the explicit region message is eligible for the
    legacy-upload fallback; authentication and anti-bot failures must remain
    visible instead of being masked by a second request.
    """
    return (
        status_code == 403
        and _REGION_RESTRICTION_MARKER in body_text.lower()
    )


def _extract_uploaded_asset(result: object) -> tuple[str, str]:
    """Extract an asset ID/URI from either current or legacy upload JSON."""
    if not isinstance(result, dict):
        return "", ""
    candidates: list[dict] = [result]
    metadata = result.get("fileMetadata")
    if isinstance(metadata, dict):
        candidates.insert(0, metadata)
    for candidate in candidates:
        file_id = str(
            candidate.get("fileMetadataId")
            or candidate.get("fileId")
            or candidate.get("id")
            or ""
        ).strip()
        if file_id:
            return file_id, str(candidate.get("fileUri") or "").strip()
    return "", ""

# Global semaphore — limits concurrent upload_file() calls across all requests.
# Initialised lazily on first call so the event loop is guaranteed to be running.
_upload_sem: asyncio.Semaphore | None = None

def _get_upload_sem() -> asyncio.Semaphore:
    global _upload_sem
    if _upload_sem is None:
        n = max(1, int(get_config("batch.asset_upload_concurrency", 10)))
        _upload_sem = asyncio.Semaphore(n)
    return _upload_sem


# ---------------------------------------------------------------------------
# File-input parsing
# ---------------------------------------------------------------------------

def _is_url(value: str) -> bool:
    try:
        p = urlparse(value)
        return bool(p.scheme in {"http", "https"} and p.netloc)
    except Exception:
        return False


def _mime_from_name(filename: str, fallback: str = "application/octet-stream") -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or fallback


def parse_data_uri(data_uri: str) -> tuple[str, str, str]:
    """Split a data URI into (filename, base64_content, mime_type).

    Raises ``ValidationError`` on invalid input.
    """
    if not data_uri.startswith("data:"):
        raise ValidationError("File input must be a URL or data URI", param="content")

    try:
        header, b64 = data_uri.split(",", 1)
    except ValueError:
        raise ValidationError("Malformed data URI: missing comma separator", param="content")

    if ";base64" not in header:
        raise ValidationError("Data URI must be base64-encoded", param="content")

    mime = header[5:].split(";", 1)[0].strip() or "application/octet-stream"
    b64  = re.sub(r"\s+", "", b64)
    if not b64:
        raise ValidationError("Data URI has empty payload", param="content")

    ext  = mime.split("/")[-1] if "/" in mime else "bin"
    return f"file.{ext}", b64, mime


# ---------------------------------------------------------------------------
# Core upload function
# ---------------------------------------------------------------------------

async def upload_file(
    token:    str,
    filename: str,
    mime:     str,
    b64:      str,
) -> tuple[str, str]:
    """Upload base64-encoded file content to Grok.

    Args:
        token:    SSO session token.
        filename: Original file name (used for content-type inference).
        mime:     MIME type string (e.g. ``"image/png"``).
        b64:      Base64-encoded file content (no data-URI prefix).

    Returns:
        ``(file_id, file_uri)`` — file_id is used as a file attachment ref.

    Raises:
        ``UpstreamError`` on HTTP failure.
    """
    async with _get_upload_sem():
        return await _upload_file_inner(token, filename, mime, b64)


async def upload_imagine_image(
    token: str,
    filename: str,
    mime: str,
    b64: str,
) -> tuple[str, str]:
    """Upload an Imagine reference image using Grok's current multipart API.

    The response returns ``fileMetadata.fileMetadataId``.  This ID is passed
    directly to the Imagine ``inputAssets`` field rather than converted into a
    content URL, matching the web UI's current request format.
    """
    if not mime.lower().startswith("image/"):
        raise ValidationError("Image edit only supports image uploads", param="image")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValidationError("Image data is not valid base64", param="image") from exc
    if not raw:
        raise ValidationError("Image data is empty", param="image")

    async with _get_upload_sem():
        return await _upload_imagine_image_inner(token, filename, mime, raw)


async def _upload_imagine_image_inner(
    token: str,
    filename: str,
    mime: str,
    raw: bytes,
) -> tuple[str, str]:
    cfg = get_config()
    timeout_s = cfg.get_float("asset.upload_timeout", 60.0)
    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()
    trace_id = start_upstream_trace(
        account_token=token,
        endpoint=_IMAGINE_DIRECT_UPLOAD_URL,
        payload={
            "operation": "imagine_reference_upload",
            "file_name": filename,
            "mime_type": mime,
            "bytes": len(raw),
        },
    )
    headers = build_http_headers(
        token,
        lease=lease,
        origin="https://grok.com",
        referer="https://grok.com/imagine",
    )
    # The HTTP client must generate a multipart boundary itself.
    headers.pop("Content-Type", None)
    kwargs = build_session_kwargs(lease=lease)

    try:
        # curl_cffi does not implement httpx/requests-style ``files=``.
        # Build a CurlMime object explicitly so it can generate the multipart
        # boundary and Content-Disposition headers expected by Grok.
        from curl_cffi import CurlMime

        multipart = CurlMime()
        multipart.addpart(
            "file_source",
            content_type="text/plain",
            data=_IMAGINE_FILE_SOURCE.encode("utf-8"),
        )
        multipart.addpart(
            "file",
            content_type=mime,
            filename=filename or "image",
            data=raw,
        )
        try:
            async with ResettableSession(**kwargs) as session:
                response = await session.post(
                    _IMAGINE_DIRECT_UPLOAD_URL,
                    headers=headers,
                    multipart=multipart,
                    timeout=timeout_s,
                )
        finally:
            multipart.close()
        body_bytes = response.content
        if response.status_code != 200:
            body_text = body_bytes.decode("utf-8", "replace")[:500]
            region_restricted = _is_imagine_region_restricted(
                response.status_code, body_text
            )
            # A region-policy 403 is not evidence that the proxy is broken or
            # that the account is invalid.  Do not cool down the egress node as
            # a challenge; try the older upload endpoint through the same
            # configured proxy instead.
            if not region_restricted:
                await proxy.feedback(
                    lease,
                    build_feedback(
                        response.status_code,
                        is_cloudflare="just a moment" in body_text.lower(),
                    ),
                )
            error = UpstreamError(
                (
                    "Grok 参考图上传被当前代理出口地区限制（HTTP 403）"
                    if region_restricted
                    else f"Imagine reference upload returned {response.status_code}"
                ),
                status=response.status_code,
                body=body_text,
            )
            fail_upstream_trace(
                trace_id,
                account_token=token,
                endpoint=_IMAGINE_DIRECT_UPLOAD_URL,
                error=error,
                status=error.status,
            )
            if region_restricted:
                logger.warning(
                    "Imagine 参考图直传被代理出口地区限制，尝试兼容上传接口：filename={!r}",
                    filename,
                )
                try:
                    legacy_id, legacy_uri = await _upload_file_inner(
                        token,
                        filename,
                        mime,
                        base64.b64encode(raw).decode("ascii"),
                    )
                    if legacy_id:
                        logger.info(
                            "Imagine 参考图已通过兼容上传接口完成：filename={!r} file_id={}",
                            filename,
                            legacy_id,
                        )
                        return legacy_id, legacy_uri
                    raise UpstreamError("兼容上传接口未返回文件 ID")
                except Exception as fallback_exc:
                    logger.warning(
                        "Imagine 参考图兼容上传接口也失败：{}",
                        fallback_exc,
                    )
                    raise UpstreamError(
                        "Grok 参考图上传失败：当前代理出口地区不支持 Imagine 直传，兼容上传接口也未成功；请更换配置的代理出口地区",
                        status=error.status,
                        body=body_text,
                    ) from fallback_exc
            raise error

        try:
            result = orjson.loads(body_bytes)
        except Exception as exc:
            raise UpstreamError("Imagine reference upload returned invalid JSON") from exc
        metadata = result.get("fileMetadata") if isinstance(result, dict) else None
        if not isinstance(metadata, dict):
            raise UpstreamError("Imagine reference upload returned no file metadata")
        file_id = str(metadata.get("fileMetadataId") or "").strip()
        file_uri = str(metadata.get("fileUri") or "").strip()
        if not file_id:
            raise UpstreamError("Imagine reference upload returned no file id")

        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
        )
        finish_upstream_trace(
            trace_id,
            account_token=token,
            endpoint=_IMAGINE_DIRECT_UPLOAD_URL,
            response={"fileMetadataId": file_id, "fileUri": file_uri},
            completed=True,
        )
        logger.info("Imagine reference upload completed: filename={!r} file_id={}", filename, file_id)
        return file_id, file_uri
    except UpstreamError as error:
        # HTTP failures were traced above. Structural success-response failures
        # still need a visible audit record.
        if not error.details.get("body"):
            fail_upstream_trace(
                trace_id,
                account_token=token,
                endpoint=_IMAGINE_DIRECT_UPLOAD_URL,
                error=error,
                status=error.status,
            )
        raise
    except Exception as exc:
        await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR))
        error = UpstreamError(f"Imagine reference upload transport error: {exc}")
        fail_upstream_trace(
            trace_id,
            account_token=token,
            endpoint=_IMAGINE_DIRECT_UPLOAD_URL,
            error=error,
            status=error.status,
        )
        raise error from exc
async def _upload_file_inner(
    token:    str,
    filename: str,
    mime:     str,
    b64:      str,
) -> tuple[str, str]:
    cfg       = get_config()
    timeout_s = cfg.get_float("asset.upload_timeout", 60.0)

    proxy = await get_proxy_runtime()
    lease = await proxy.acquire()

    payload = orjson.dumps({
        "fileName":     filename,
        "fileMimeType": mime,
        "content":      b64,
    })
    trace_id = start_upstream_trace(
        account_token=token,
        endpoint=_UPLOAD_URL,
        payload={
            "operation": "asset_upload",
            "file_name": filename,
            "mime_type": mime,
            "base64_chars": len(b64),
        },
    )
    headers = build_http_headers(token, lease=lease)
    kwargs  = build_session_kwargs(lease=lease)

    try:
        async with ResettableSession(**kwargs) as session:
            response = await session.post(
                _UPLOAD_URL,
                headers = headers,
                data    = payload,
                timeout = timeout_s,
            )

        body_bytes = response.content
        if response.status_code != 200:
            body_text = body_bytes.decode("utf-8", "replace")[:300]
            logger.error(
                "asset upload request failed: status={} body={}",
                response.status_code, body_text,
            )
            is_cloudflare = "just a moment" in body_text.lower()
            await proxy.feedback(
                lease,
                build_feedback(response.status_code, is_cloudflare=is_cloudflare),
            )
            error = UpstreamError(
                f"Asset upload returned {response.status_code}",
                status = response.status_code,
                body   = body_text,
            )
            fail_upstream_trace(
                trace_id,
                account_token=token,
                endpoint=_UPLOAD_URL,
                error=error,
                status=response.status_code,
            )
            raise error

        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS, status_code=200),
        )

        try:
            result = orjson.loads(body_bytes)
        except Exception as exc:
            raise UpstreamError("Asset upload returned invalid JSON") from exc
        file_id, file_uri = _extract_uploaded_asset(result)
        if not file_id:
            raise UpstreamError("Asset upload returned no file ID")
        finish_upstream_trace(
            trace_id,
            account_token=token,
            endpoint=_UPLOAD_URL,
            response=result,
            completed=True,
        )
        logger.info("asset upload completed: filename={!r} file_id={}", filename, file_id)
        return file_id, file_uri

    except UpstreamError as error:
        # HTTP failures are traced at the response branch above.  Structural
        # failures (invalid JSON or a missing ID) still need an audit record.
        if not error.details.get("body"):
            fail_upstream_trace(
                trace_id,
                account_token=token,
                endpoint=_UPLOAD_URL,
                error=error,
                status=error.status,
            )
        raise
    except Exception as exc:
        await proxy.feedback(
            lease,
            ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR),
        )
        error = UpstreamError(f"Asset upload transport error: {exc}")
        fail_upstream_trace(
            trace_id,
            account_token=token,
            endpoint=_UPLOAD_URL,
            error=error,
            status=error.status,
        )
        raise error from exc


async def upload_imagine_from_input(token: str, file_input: str) -> tuple[str, str]:
    """Resolve a URL/data URI and upload it as an Imagine reference image."""
    if _is_url(file_input):
        proxy = await get_proxy_runtime()
        lease = await proxy.acquire()
        try:
            headers = build_http_headers(token, lease=lease)
            kwargs = build_session_kwargs(lease=lease)
            async with ResettableSession(**kwargs) as session:
                response = await session.get(file_input, headers=headers, timeout=30.0)
            raw = response.content
            if response.status_code != 200:
                await proxy.feedback(
                    lease,
                    ProxyFeedback(
                        kind=(
                            ProxyFeedbackKind.UPSTREAM_5XX
                            if response.status_code >= 500
                            else ProxyFeedbackKind.FORBIDDEN
                        ),
                        status_code=response.status_code,
                    ),
                )
                raise UpstreamError(
                    f"Failed to fetch input URL: {response.status_code}",
                    status=response.status_code,
                )
            mime = (
                response.headers.get("content-type", "").split(";", 1)[0].strip()
                or "application/octet-stream"
            )
            filename = file_input.split("/")[-1].split("?", 1)[0] or "image"
            b64 = base64.b64encode(raw).decode()
        except UpstreamError:
            raise
        except Exception as exc:
            await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR))
            raise UpstreamError(f"Asset fetch transport error: {exc}") from exc
        await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS))
        return await upload_imagine_image(token, filename, mime, b64)

    filename, b64, mime = parse_data_uri(file_input)
    return await upload_imagine_image(token, filename, mime, b64)


async def upload_from_input(token: str, file_input: str) -> tuple[str, str]:
    """High-level helper: parse *file_input* (URL or data URI) and upload.

    Returns ``(file_id, file_uri)``.
    """
    if _is_url(file_input):
        # Fetch the remote URL and re-upload as base64.
        proxy = await get_proxy_runtime()
        lease = await proxy.acquire()
        try:
            headers = build_http_headers(token, lease=lease)
            kwargs  = build_session_kwargs(lease=lease)
            async with ResettableSession(**kwargs) as session:
                resp = await session.get(file_input, headers=headers, timeout=30.0)
            raw  = resp.content
            if resp.status_code != 200:
                await proxy.feedback(
                    lease,
                    ProxyFeedback(
                        kind        = ProxyFeedbackKind.UPSTREAM_5XX if resp.status_code >= 500
                                      else ProxyFeedbackKind.FORBIDDEN,
                        status_code = resp.status_code,
                    ),
                )
                raise UpstreamError(
                    f"Failed to fetch input URL: {resp.status_code}",
                    status = resp.status_code,
                )
            mime     = (resp.headers.get("content-type", "").split(";")[0].strip()
                        or "application/octet-stream")
            filename = file_input.split("/")[-1].split("?")[0] or "download"
            b64      = base64.b64encode(raw).decode()
        except UpstreamError:
            raise
        except Exception as exc:
            await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.TRANSPORT_ERROR))
            raise UpstreamError(f"Asset fetch transport error: {exc}") from exc

        await proxy.feedback(lease, ProxyFeedback(kind=ProxyFeedbackKind.SUCCESS))
        return await upload_file(token, filename, mime, b64)

    # Data URI
    filename, b64, mime = parse_data_uri(file_input)
    return await upload_file(token, filename, mime, b64)


def resolve_uploaded_asset_reference(token: str, file_id: str, file_uri: str) -> str:
    """Resolve an uploaded asset to the content URL required by image-edit."""
    user_id = _extract_user_id(token)
    url = resolve_asset_reference(file_id, file_uri, user_id=user_id)
    if url:
        return url
    raise UpstreamError("Could not resolve uploaded asset reference URL")


def _extract_user_id(token: str) -> str | None:
    cookie = build_sso_cookie(token)
    match = _X_USER_ID_RE.search(cookie)
    if match:
        return match.group(1)
    return None


__all__ = [
    "upload_file",
    "upload_imagine_image",
    "upload_imagine_from_input",
    "upload_from_input",
    "parse_data_uri",
    "resolve_uploaded_asset_reference",
]
