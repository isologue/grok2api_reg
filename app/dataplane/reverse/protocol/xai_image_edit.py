"""XAI image-edit protocol — payload builder and SSE field extractors."""

from typing import Any

IMAGE_EDIT_MODEL_NAME = "imagine-image-edit"
IMAGE_EDIT_MODEL_KIND = "imagine"
IMAGE_POST_MEDIA_TYPE = "MEDIA_POST_TYPE_IMAGE"
IMAGE_EDIT_GENERATION_COUNT = 2


def build_image_edit_payload(
    *,
    prompt: str,
    input_assets: list[str],
) -> dict[str, Any]:
    """Build a current Imagine image-to-image payload.

    The Grok web application uploads each reference with
    ``/http/upload-file-v2/direct`` and sends its ``fileMetadataId`` in
    ``mediaGenInput.imageToImage.inputAssets``.  The old ``imageReferences`` /
    ``parentPostId`` configuration is no longer accepted reliably upstream.
    """
    return {
        "modelName": IMAGE_EDIT_MODEL_NAME,
        "message": prompt,
        "enableImageStreaming": True,
        "enableSideBySide": True,
        "sendFinalMetadata": True,
        "responseMetadata": {
            "modelConfigOverride": {
                "modelMap": {"imageEditModel": IMAGE_EDIT_MODEL_KIND}
            }
        },
        "mediaGenInput": {
            "imageToImage": {
                "prompt": prompt,
                "inputAssets": input_assets,
            }
        },
        "kind": "CONVERSATION_KIND_IMAGINE",
    }


def extract_streaming_response(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return ``response.streamingImageGenerationResponse`` when present."""
    result = data.get("result")
    if not isinstance(result, dict):
        return None
    response = result.get("response")
    if not isinstance(response, dict):
        return None
    stream = response.get("streamingImageGenerationResponse")
    return stream if isinstance(stream, dict) else None


def extract_model_response_urls(data: dict[str, Any]) -> list[str]:
    """Return fallback ``modelResponse.generatedImageUrls`` values."""
    result = data.get("result")
    if not isinstance(result, dict):
        return []
    response = result.get("response")
    if not isinstance(response, dict):
        return []
    model_response = response.get("modelResponse")
    if not isinstance(model_response, dict):
        return []
    urls = model_response.get("generatedImageUrls")
    if not isinstance(urls, list):
        return []
    return [url for url in urls if isinstance(url, str) and url]


def extract_model_response_file_attachments(data: dict[str, Any]) -> list[str]:
    """Return fallback ``modelResponse.fileAttachments`` asset IDs."""
    result = data.get("result")
    if not isinstance(result, dict):
        return []
    response = result.get("response")
    if not isinstance(response, dict):
        return []
    model_response = response.get("modelResponse")
    if not isinstance(model_response, dict):
        return []
    attachments = model_response.get("fileAttachments")
    if not isinstance(attachments, list):
        return []
    return [attachment for attachment in attachments if isinstance(attachment, str) and attachment]


__all__ = [
    "IMAGE_EDIT_MODEL_NAME",
    "IMAGE_EDIT_MODEL_KIND",
    "IMAGE_POST_MEDIA_TYPE",
    "IMAGE_EDIT_GENERATION_COUNT",
    "build_image_edit_payload",
    "extract_streaming_response",
    "extract_model_response_urls",
    "extract_model_response_file_attachments",
]
