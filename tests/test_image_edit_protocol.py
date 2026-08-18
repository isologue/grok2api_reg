import unittest

from app.dataplane.reverse.protocol.xai_image_edit import build_image_edit_payload


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


if __name__ == "__main__":
    unittest.main()
