from __future__ import annotations

import unittest


class SseParserTests(unittest.TestCase):
    def parser(self):
        from translation_platform.sse import SseParser

        return SseParser(content_path=("content",))

    def test_data_events_are_joined_until_done_marker(self) -> None:
        result = self.parser().parse_lines(
            [
                ": synthetic keep-alive",
                'data: {"content":"Hello"}',
                "",
                'data:{"content":" world"}',
                "",
                "data: [DONE]",
                "",
                'data: {"content":"ignored"}',
            ]
        )

        self.assertEqual(result.content, "Hello world")
        self.assertEqual(result.event_count, 2)
        self.assertTrue(result.completed)

    def test_json_escaping_and_braces_inside_content_are_preserved(self) -> None:
        result = self.parser().parse_lines(
            [
                r'data: {"content":"line 1\nquoted: \"yes\" and brace }"}',
                "",
                "data: [DONE]",
            ]
        )

        self.assertEqual(result.content, 'line 1\nquoted: "yes" and brace }')

    def test_multiple_data_lines_form_one_json_event(self) -> None:
        result = self.parser().parse_lines(
            [
                'data: {"content":',
                'data: "synthetic chunk"}',
                "",
                "data: [DONE]",
            ]
        )

        self.assertEqual(result.content, "synthetic chunk")
        self.assertEqual(result.event_count, 1)

    def test_bytes_metadata_and_end_of_stream_flush_are_supported(self) -> None:
        result = self.parser().parse_lines(
            [
                b"event: message",
                b'data: {"meta":"synthetic"}',
                b"",
                'data: {"content":"final chunk"}',
            ]
        )

        self.assertEqual(result.content, "final chunk")
        self.assertEqual(result.event_count, 2)
        self.assertFalse(result.completed)

    def test_server_error_event_raises_classified_exception(self) -> None:
        from translation_platform.sse import SseServerError

        with self.assertRaisesRegex(SseServerError, "synthetic unavailable") as caught:
            self.parser().parse_lines(
                [
                    'data: {"error":{"code":"SYNTHETIC_503","message":"synthetic unavailable"}}',
                    "",
                ]
            )

        self.assertEqual(caught.exception.server_code, "SYNTHETIC_503")

    def test_malformed_json_invalid_utf8_and_empty_content_are_distinct(self) -> None:
        from translation_platform.sse import (
            SseContentError,
            SseDecodeError,
            SseJsonError,
        )

        cases = (
            (["data: {invalid}", ""], SseJsonError, "event 1"),
            ([b"data: \xff", b""], SseDecodeError, "UTF-8"),
            (["data: [DONE]"], SseContentError, "no content"),
        )
        for lines, error_type, message in cases:
            with self.subTest(error=error_type.__name__):
                with self.assertRaisesRegex(error_type, message):
                    self.parser().parse_lines(lines)

    def test_nested_content_path_and_type_error_are_deterministic(self) -> None:
        from translation_platform.sse import SseContentError, SseParser

        parser = SseParser(content_path=("data", "delta", "content"))
        result = parser.parse_lines(
            [
                'data: {"data":{"delta":{"content":"nested"}}}',
                "",
            ]
        )
        self.assertEqual(result.content, "nested")

        with self.assertRaisesRegex(SseContentError, "must be text"):
            parser.parse_lines(
                [
                    'data: {"data":{"delta":{"content":123}}}',
                    "",
                ]
            )

    def test_parse_response_uses_requests_compatible_iter_lines(self) -> None:
        class FakeStreamingResponse:
            def __init__(self) -> None:
                self.decode_unicode = None

            def iter_lines(self, decode_unicode=False):
                self.decode_unicode = decode_unicode
                return iter(
                    [
                        b'data: {"content":"from response"}',
                        b"",
                        b"data: [DONE]",
                    ]
                )

        response = FakeStreamingResponse()
        result = self.parser().parse_response(response)

        self.assertEqual(result.content, "from response")
        self.assertTrue(result.completed)
        self.assertFalse(response.decode_unicode)


if __name__ == "__main__":
    unittest.main()
