import unittest
from app.products.openai.router import _has_image_reference

class TestImageReferenceDispatch(unittest.TestCase):
    def test_detects_data_uri_image_block(self):
        self.assertTrue(_has_image_reference([{"role":"user","content":[{"type":"text","text":"edit"},{"type":"image_url","image_url":{"url":"data:image/png;base64,AA=="}}]}]))

    def test_text_only_stays_generation(self):
        self.assertFalse(_has_image_reference([{"role":"user","content":"draw a cat"}]))

if __name__ == '__main__':
    unittest.main()
