from __future__ import annotations

import unittest


def make_test_rate_limiter():
    """构造无需真实等待、但仍按时间槽推进的测试限速器。"""

    from translation_platform.pacing import GlobalRateLimiter

    state = [0.0]

    def monotonic() -> float:
        return state[0]

    def sleep(delay: float) -> None:
        state[0] += delay

    return GlobalRateLimiter(
        min_interval_seconds=1.0,
        monotonic=monotonic,
        sleep=sleep,
    )


class FakeResponse:
    def __init__(
        self,
        payload=None,
        status_error=None,
        json_error=None,
        status_code=200,
        stream_lines=None,
        headers=None,
    ) -> None:
        self.payload = {} if payload is None else payload
        self.status_error = status_error
        self.json_error = json_error
        self.status_code = status_code
        self.stream_lines = stream_lines
        self.headers = {} if headers is None else dict(headers)
        self.status_checks = 0
        self.closed = False

    def raise_for_status(self) -> None:
        self.status_checks += 1
        if self.status_error is not None:
            raise self.status_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    def iter_lines(self, decode_unicode=False):
        if self.stream_lines is None:
            raise RuntimeError("synthetic response is not streaming")
        return iter(self.stream_lines)

    def close(self) -> None:
        self.closed = True


class RecordingSession:
    def __init__(self, response=None, error=None) -> None:
        self.response = response if response is not None else FakeResponse()
        self.error = error
        self.calls = []
        self.headers = {}
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response

    def close(self) -> None:
        self.closed = True


class ClientTestCase(unittest.TestCase):
    def signed_chat_request(self):
        from translation_platform.protocol import EndpointKind, SignedRequest
        from translation_platform.signer import SignatureResult

        return SignedRequest(
            endpoint=EndpointKind.CHAT,
            parameters={
                "input": "synthetic text",
                "mysticTime": "1700000000123",
                "sign": "synthetic-signature",
            },
            signature=SignatureResult(
                mystic_time="1700000000123",
                payload="synthetic-payload",
                signature="synthetic-signature",
            ),
        )

    def make_clients(self, session):
        from translation_platform.client import BusinessRequestClient, SessionHttpClient

        transport = SessionHttpClient(
            session=session,
            rate_limiter=make_test_rate_limiter(),
        )
        business = BusinessRequestClient(
            transport=transport,
            business_url="https://translation.invalid/chat",
        )
        return transport, business


class BusinessPostTests(ClientTestCase):
    def test_one_translation_sends_exactly_one_business_post(self) -> None:
        response = FakeResponse(payload={"synthetic": "response"})
        session = RecordingSession(response=response)
        _, client = self.make_clients(session)

        actual = client.post_translation(self.signed_chat_request())

        self.assertIs(actual, response)
        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://translation.invalid/chat")
        self.assertEqual(kwargs["params"]["mysticTime"], "1700000000123")
        self.assertTrue(kwargs["stream"])
        self.assertEqual(response.status_checks, 1)

    def test_business_post_can_send_signed_parameters_as_form_data(self) -> None:
        from translation_platform.client import BusinessRequestClient, SessionHttpClient

        response = FakeResponse(payload={"synthetic": "response"})
        session = RecordingSession(response=response)
        transport = SessionHttpClient(
            session=session,
            rate_limiter=make_test_rate_limiter(),
        )
        client = BusinessRequestClient(
            transport=transport,
            business_url="https://translation.invalid/chat",
            parameter_location="form",
        )

        client.post_translation(self.signed_chat_request())

        _, kwargs = session.calls[0]
        self.assertEqual(kwargs["data"]["input"], "synthetic text")
        self.assertNotIn("params", kwargs)
        self.assertTrue(kwargs["stream"])

    def test_failed_post_is_reported_once_without_hidden_second_request(self) -> None:
        from translation_platform.client import BusinessPostError

        cause = OSError("synthetic connection failure")
        session = RecordingSession(error=cause)
        _, client = self.make_clients(session)

        with self.assertRaisesRegex(BusinessPostError, "business POST failed") as caught:
            client.post_translation(self.signed_chat_request())

        self.assertEqual(len(session.calls), 1)
        self.assertIs(caught.exception.__cause__, cause)

    def test_non_chat_request_is_rejected_before_posting(self) -> None:
        from translation_platform.client import BusinessPostError
        from translation_platform.protocol import EndpointKind, SignedRequest

        source = self.signed_chat_request()
        secret_request = SignedRequest(
            endpoint=EndpointKind.SECRET,
            parameters=source.parameters,
            signature=source.signature,
        )
        session = RecordingSession()
        _, client = self.make_clients(session)

        with self.assertRaisesRegex(BusinessPostError, "chat request"):
            client.post_translation(secret_request)

        self.assertEqual(session.calls, [])

    def test_401_is_authentication_and_403_is_access_restriction(self) -> None:
        from translation_platform.errors import AccessRestrictionError
        from translation_platform.token import AuthenticationFailure

        cases = ((401, AuthenticationFailure), (403, AccessRestrictionError))
        for status_code, error_type in cases:
            with self.subTest(status_code=status_code):
                response = FakeResponse(
                    status_code=status_code,
                    status_error=RuntimeError(f"synthetic {status_code}"),
                )
                session = RecordingSession(response=response)
                _, client = self.make_clients(session)

                with self.assertRaises(error_type):
                    client.post_translation(self.signed_chat_request())

                self.assertEqual(len(session.calls), 1)


class SessionHttpClientTests(unittest.TestCase):
    def test_headers_and_connect_read_timeouts_are_applied_consistently(self) -> None:
        from translation_platform.client import SessionHttpClient

        response = FakeResponse(payload={"token": "synthetic-token"})
        session = RecordingSession(response=response)
        client = SessionHttpClient(
            session=session,
            rate_limiter=make_test_rate_limiter(),
            headers={"X-Synthetic-Client": "test"},
            connect_timeout=2.5,
            read_timeout=8.0,
        )

        payload = client.post_json(
            "https://translation.invalid/secret",
            params={"keyid": "synthetic-keyid"},
        )

        self.assertEqual(payload, {"token": "synthetic-token"})
        self.assertEqual(session.headers["Accept"], "application/json, text/event-stream")
        self.assertEqual(session.headers["X-Synthetic-Client"], "test")
        _, kwargs = session.calls[0]
        self.assertEqual(kwargs["timeout"], (2.5, 8.0))
        self.assertFalse(kwargs["stream"])
        self.assertEqual(response.status_checks, 1)

    def test_status_json_decode_and_json_shape_failures_are_distinct(self) -> None:
        from translation_platform.client import HttpStatusError, JsonResponseError, SessionHttpClient

        cases = (
            (FakeResponse(status_error=RuntimeError("synthetic 503")), HttpStatusError, "status check failed"),
            (FakeResponse(json_error=ValueError("synthetic invalid JSON")), JsonResponseError, "invalid JSON"),
            (FakeResponse(payload=["not", "an", "object"]), JsonResponseError, "JSON object"),
        )
        for response, error_type, message in cases:
            with self.subTest(message=message):
                client = SessionHttpClient(
                    session=RecordingSession(response=response),
                    rate_limiter=make_test_rate_limiter(),
                )
                with self.assertRaisesRegex(error_type, message):
                    client.post_json("https://translation.invalid/secret", params={})

    def test_status_failure_closes_response_before_raising(self) -> None:
        from translation_platform.client import HttpStatusError, SessionHttpClient

        response = FakeResponse(
            status_code=503,
            status_error=RuntimeError("synthetic 503"),
        )
        client = SessionHttpClient(
            session=RecordingSession(response=response),
            rate_limiter=make_test_rate_limiter(),
        )

        with self.assertRaises(HttpStatusError):
            client.post("https://translation.invalid/chat", params={}, stream=True)

        self.assertTrue(response.closed)

    def test_context_manager_and_explicit_close_own_session_lifecycle(self) -> None:
        from translation_platform.client import ClientClosedError, SessionHttpClient

        session = RecordingSession()
        with SessionHttpClient(
            session=session,
            rate_limiter=make_test_rate_limiter(),
        ) as client:
            self.assertIs(client.session, session)
        self.assertTrue(session.closed)

        client.close()
        with self.assertRaisesRegex(ClientClosedError, "closed"):
            client.post_json("https://translation.invalid/secret", params={})

    def test_invalid_timeouts_are_rejected_before_any_request(self) -> None:
        from translation_platform.client import HttpClientConfigurationError, SessionHttpClient

        for connect_timeout, read_timeout in ((0, 1), (1, 0), (True, 1), (1, "5")):
            with self.subTest(connect=connect_timeout, read=read_timeout):
                with self.assertRaises(HttpClientConfigurationError):
                    SessionHttpClient(
                        session=RecordingSession(),
                        rate_limiter=make_test_rate_limiter(),
                        connect_timeout=connect_timeout,
                        read_timeout=read_timeout,
                    )


if __name__ == "__main__":
    unittest.main()
