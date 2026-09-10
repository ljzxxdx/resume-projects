from __future__ import annotations

import unittest

import requests

from tests.test_client import FakeResponse, RecordingSession, make_test_rate_limiter


class ErrorHierarchyTests(unittest.TestCase):
    def test_required_error_categories_have_distinct_types(self) -> None:
        from translation_platform.errors import (
            ConfigurationFailure,
            EmptyTranslationError,
            ErrorType,
            Http4xxError,
            Http5xxError,
            NetworkConnectionError,
            RateLimitError,
            RequestTimeoutError,
            ResponseFormatError,
            SignatureTokenFailure,
        )

        cases = (
            (ConfigurationFailure("synthetic"), ErrorType.CONFIGURATION),
            (SignatureTokenFailure("synthetic"), ErrorType.SIGNATURE_TOKEN),
            (NetworkConnectionError("synthetic"), ErrorType.NETWORK_CONNECTION),
            (RequestTimeoutError("synthetic"), ErrorType.TIMEOUT),
            (Http4xxError("synthetic", status_code=400), ErrorType.HTTP_4XX),
            (Http5xxError("synthetic", status_code=503), ErrorType.HTTP_5XX),
            (RateLimitError("synthetic", status_code=429), ErrorType.RATE_LIMIT),
            (ResponseFormatError("synthetic"), ErrorType.RESPONSE_FORMAT),
            (EmptyTranslationError("synthetic"), ErrorType.EMPTY_RESULT),
        )

        self.assertEqual(len({type(error) for error, _ in cases}), 9)
        for error, expected_type in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(error.error_type, expected_type)

    def test_existing_domain_errors_join_the_common_hierarchy(self) -> None:
        from translation_platform.config import ConfigurationError
        from translation_platform.errors import (
            ConfigurationFailure,
            ErrorType,
            ResponseFormatError,
            SignatureTokenFailure,
            TranslationPlatformError,
        )
        from translation_platform.protocol import ProtocolError
        from translation_platform.signer import SignerError
        from translation_platform.sse import SseError
        from translation_platform.token import TokenError
        from translation_platform.models import ModelValidationError

        errors = (
            ConfigurationError("synthetic"),
            ProtocolError("synthetic"),
            SignerError("synthetic"),
            SseError("synthetic"),
            TokenError("synthetic"),
            ModelValidationError("synthetic"),
        )
        self.assertTrue(all(isinstance(error, TranslationPlatformError) for error in errors))
        self.assertIsInstance(errors[0], ConfigurationFailure)
        self.assertTrue(all(isinstance(error, SignatureTokenFailure) for error in errors[1:3]))
        self.assertIsInstance(errors[3], ResponseFormatError)
        self.assertIsInstance(errors[4], SignatureTokenFailure)
        self.assertIsInstance(errors[5], ConfigurationFailure)
        self.assertEqual(errors[5].error_type, ErrorType.CONFIGURATION)


class RealPathClassificationTests(unittest.TestCase):
    def client_for(self, session):
        from translation_platform.client import SessionHttpClient

        return SessionHttpClient(
            session=session,
            rate_limiter=make_test_rate_limiter(),
        )

    def test_connection_and_timeout_exceptions_are_classified(self) -> None:
        from translation_platform.errors import NetworkConnectionError, RequestTimeoutError

        cases = (
            (requests.ConnectionError("synthetic connection"), NetworkConnectionError),
            (requests.Timeout("synthetic timeout"), RequestTimeoutError),
        )
        for cause, expected_error in cases:
            with self.subTest(error=expected_error.__name__):
                client = self.client_for(RecordingSession(error=cause))
                with self.assertRaises(expected_error) as caught:
                    client.post("https://translation.invalid/chat", params={})
                self.assertIs(caught.exception.__cause__, cause)

    def test_http_status_families_are_classified_and_keep_status(self) -> None:
        from translation_platform.errors import Http4xxError, Http5xxError, RateLimitError

        cases = (
            (400, Http4xxError),
            (429, RateLimitError),
            (503, Http5xxError),
        )
        for status_code, expected_error in cases:
            with self.subTest(status_code=status_code):
                response = FakeResponse(
                    status_code=status_code,
                    status_error=requests.HTTPError(f"synthetic {status_code}"),
                )
                client = self.client_for(RecordingSession(response=response))
                with self.assertRaises(expected_error) as caught:
                    client.post("https://translation.invalid/chat", params={})
                self.assertEqual(caught.exception.status_code, status_code)
                self.assertTrue(response.closed)

    def test_empty_sse_content_uses_empty_result_category(self) -> None:
        from translation_platform.errors import EmptyTranslationError, ErrorType
        from translation_platform.sse import SseParser

        with self.assertRaises(EmptyTranslationError) as caught:
            SseParser().parse_lines(["data: [DONE]"])

        self.assertEqual(caught.exception.error_type, ErrorType.EMPTY_RESULT)


if __name__ == "__main__":
    unittest.main()
