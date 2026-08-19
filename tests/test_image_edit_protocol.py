import unittest

from app.dataplane.reverse.protocol.xai_image_edit import build_image_edit_payload
from app.products.openai.images import _normalize_edit_size


class ImageEditProtocolTests(unittest.TestCase):
    def test_payload_matches_current_imagine_image_to_image_shape(self) -> None:
        payload = build_image_edit_payload(
            prompt="dress the model in the reference garment",
            input_assets=["asset-1", "asset-2"],
        )

        self.assertEqual(payload["modelName"], "imagine-image-edit")
        self.assertEqual(payload["kind"], "CONVERSATION_KIND_IMAGINE")
        self.assertEqual(
            payload["responseMetadata"]["modelConfigOverride"]["modelMap"],
            {"imageEditModel": "imagine"},
        )
        self.assertEqual(
            payload["mediaGenInput"]["imageToImage"],
            {
                "prompt": "dress the model in the reference garment",
                "inputAssets": ["asset-1", "asset-2"],
            },
        )
        self.assertNotIn("imageReferences", str(payload))
        self.assertNotIn("parentPostId", str(payload))

    def test_edit_size_is_accepted_without_local_1024_square_restriction(self) -> None:
        # The browser's image-to-image protocol has no size field.  Accept
        # OpenAI/canvas sizes rather than rejecting non-square references here.
        self.assertEqual(_normalize_edit_size("1536x1024"), "1536x1024")
        self.assertEqual(_normalize_edit_size("720x1280"), "720x1280")
        self.assertEqual(_normalize_edit_size("16:9"), "16:9")


if __name__ == "__main__":
    unittest.main()
