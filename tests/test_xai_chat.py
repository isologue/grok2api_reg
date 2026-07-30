import unittest

from app.dataplane.reverse.protocol.xai_chat import stream_error_from_payload


class StreamErrorPayloadTests(unittest.TestCase):
    def test_nested_usage_limit_error_is_exposed_as_rate_limit(self) -> None:
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
        error = stream_error_from_payload(payload)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.status, 429)
        self.assertIn("usage limit", str(error).lower())

    def test_nested_non_quota_stream_error_is_upstream_failure(self) -> None:
        payload = {
            "result": {
                "response": {
                    "modelResponse": {
                        "metadata": {
                            "stream_errors": [{"message": "Image renderer failed"}],
                        },
                    },
                },
            },
        }
        error = stream_error_from_payload(payload)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.status, 502)

    def test_nested_load_shed_error_is_retryable_service_unavailable(self) -> None:
        payload = {
            "result": {
                "response": {
                    "modelResponse": {
                        "streamErrors": [{
                            "message": "Service temporarily unavailable. Please try again later.",
                            "systemKillSwitch": {"featureName": "sampling_load_shed"},
                        }],
                    },
                },
            },
        }
        error = stream_error_from_payload(payload)
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.status, 503)


if __name__ == "__main__":
    unittest.main()
