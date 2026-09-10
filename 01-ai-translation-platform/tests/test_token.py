from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor


class SequenceProvider:
    def __init__(self, values) -> None:
        self.values = iter(values)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value


class SequenceSession:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.calls = []
        self.headers = {}
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)

    def close(self) -> None:
        self.closed = True


class TokenManagerTests(unittest.TestCase):
    def test_token_is_loaded_lazily_and_cached(self) -> None:
        from translation_platform.token import TokenManager

        provider = SequenceProvider(["synthetic-token"])
        manager = TokenManager(provider)
        self.assertEqual(provider.calls, 0)

        self.assertEqual(manager.get_token(), "synthetic-token")
        self.assertEqual(manager.get_token(), "synthetic-token")
        self.assertEqual(provider.calls, 1)

    def test_authentication_failure_refreshes_once_then_succeeds(self) -> None:
        from translation_platform.token import AuthenticationFailure, TokenManager

        provider = SequenceProvider(["stale-token", "fresh-token"])
        manager = TokenManager(provider)
        operation_tokens = []

        def operation(token: str) -> str:
            operation_tokens.append(token)
            if token == "stale-token":
                raise AuthenticationFailure("synthetic 401")
            return "synthetic translation"

        self.assertEqual(
            manager.execute_with_token(operation),
            "synthetic translation",
        )
        self.assertEqual(operation_tokens, ["stale-token", "fresh-token"])
        self.assertEqual(provider.calls, 2)
        self.assertEqual(manager.get_token(), "fresh-token")

    def test_second_authentication_failure_terminates_and_is_classified(self) -> None:
        from translation_platform.token import (
            AuthenticationFailure,
            TokenManager,
            TokenRefreshExhausted,
        )

        provider = SequenceProvider(["stale-token", "still-invalid-token", "next-token"])
        manager = TokenManager(provider)
        attempts = []

        def operation(token: str) -> None:
            attempts.append(token)
            raise AuthenticationFailure("synthetic 403")

        with self.assertRaisesRegex(TokenRefreshExhausted, "one refresh"):
            manager.execute_with_token(operation)

        self.assertEqual(attempts, ["stale-token", "still-invalid-token"])
        self.assertEqual(provider.calls, 2)
        self.assertEqual(manager.get_token(), "next-token")

    def test_refresh_loader_failure_terminates_without_second_operation(self) -> None:
        from translation_platform.token import (
            AuthenticationFailure,
            TokenManager,
            TokenRefreshExhausted,
        )

        provider = SequenceProvider(
            ["stale-token", RuntimeError("synthetic token endpoint failure")]
        )
        manager = TokenManager(provider)
        attempts = []

        def operation(token: str) -> None:
            attempts.append(token)
            raise AuthenticationFailure("synthetic 401")

        with self.assertRaisesRegex(TokenRefreshExhausted, "refresh failed"):
            manager.execute_with_token(operation)

        self.assertEqual(attempts, ["stale-token"])
        self.assertEqual(provider.calls, 2)

    def test_refresh_exhaustion_keeps_the_final_request_proxy_summary(self) -> None:
        from translation_platform.token import (
            AuthenticationFailure,
            TokenManager,
            TokenRefreshExhausted,
        )

        provider = SequenceProvider(["stale-token", "fresh-token"])
        manager = TokenManager(provider)
        proxy_ids = iter(("proxy-000000000001", "proxy-000000000002"))

        def operation(token: str) -> None:
            error = AuthenticationFailure("synthetic 401", status_code=401)
            error.proxy_id_summary = next(proxy_ids)
            raise error

        with self.assertRaises(TokenRefreshExhausted) as caught:
            manager.execute_with_token(operation)

        self.assertEqual(
            caught.exception.proxy_id_summary,
            "proxy-000000000002",
        )

    def test_non_authentication_errors_do_not_refresh(self) -> None:
        from translation_platform.token import TokenManager

        provider = SequenceProvider(["synthetic-token", "unused-token"])
        manager = TokenManager(provider)
        cause = ValueError("synthetic response error")

        with self.assertRaises(ValueError) as caught:
            manager.execute_with_token(lambda token: (_ for _ in ()).throw(cause))

        self.assertIs(caught.exception, cause)
        self.assertEqual(provider.calls, 1)

    def test_concurrent_first_use_loads_token_once(self) -> None:
        from translation_platform.token import TokenManager

        provider_lock = threading.Lock()
        calls = []

        def provider() -> str:
            with provider_lock:
                calls.append(1)
            return "synthetic-token"

        manager = TokenManager(provider)
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _: manager.get_token(), range(16)))

        self.assertEqual(results, ["synthetic-token"] * 16)
        self.assertEqual(len(calls), 1)


class AuthenticatedClientIntegrationTests(unittest.TestCase):
    def profile(self, endpoint):
        from translation_platform.protocol import EndpointKind, EndpointSigningProfile

        if endpoint is EndpointKind.SECRET:
            return EndpointSigningProfile(
                endpoint=endpoint,
                keyid="synthetic-secret-keyid",
                signing_key="synthetic-secret-key",
                static_parameters={"client": "synthetic-client"},
                signing_fields=("client", "mysticTime", "key"),
                point_param_fields=("client", "mysticTime"),
            )
        return EndpointSigningProfile(
            endpoint=endpoint,
            keyid="synthetic-chat-keyid",
            signing_key="synthetic-chat-key",
            static_parameters={"client": "synthetic-client"},
            signing_fields=(
                "client",
                "keyid",
                "mysticTime",
                "token",
                "yduuid",
                "key",
            ),
            point_param_fields=(
                "client",
                "keyid",
                "mysticTime",
                "token",
                "yduuid",
                "key",
            ),
        )

    def make_client(self, responses):
        from translation_platform.client import (
            AuthenticatedTranslationClient,
            BusinessRequestClient,
            JsonTokenProvider,
            SessionHttpClient,
        )
        from translation_platform.protocol import EndpointKind, SignedRequestBuilder
        from translation_platform.signer import JsSigner
        from translation_platform.sse import SseParser
        from translation_platform.token import TokenManager

        session = SequenceSession(responses)
        from tests.test_client import make_test_rate_limiter

        transport = SessionHttpClient(
            session=session,
            rate_limiter=make_test_rate_limiter(),
        )
        builder = SignedRequestBuilder(JsSigner(clock=lambda: 1700000000123))
        provider = JsonTokenProvider(
            transport=transport,
            request_builder=builder,
            profile=self.profile(EndpointKind.SECRET),
            secret_url="https://translation.invalid/secret",
            yduuid="synthetic-device-id",
        )
        token_manager = TokenManager(provider)
        business = BusinessRequestClient(
            transport=transport,
            business_url="https://translation.invalid/chat",
        )
        client = AuthenticatedTranslationClient(
            token_manager=token_manager,
            request_builder=builder,
            chat_profile=self.profile(EndpointKind.CHAT),
            business_client=business,
            yduuid="synthetic-device-id",
            sse_parser=SseParser(content_path=("content",)),
        )
        return session, client

    def test_real_client_path_lazily_loads_and_refreshes_after_401(self) -> None:
        from tests.test_client import FakeResponse

        responses = [
            FakeResponse(payload={"token": "stale-token"}),
            FakeResponse(status_code=401, status_error=RuntimeError("synthetic 401")),
            FakeResponse(payload={"token": "fresh-token"}),
            FakeResponse(
                stream_lines=[
                    b'data: {"content":"synthetic "}',
                    b"",
                    b'data: {"content":"translation"}',
                    b"",
                    b"data: [DONE]",
                ]
            ),
        ]
        session, client = self.make_client(responses)
        self.assertEqual(session.calls, [])

        result = client.post_translation({"input": "synthetic text"})

        self.assertEqual(result.content, "synthetic translation")
        self.assertTrue(result.completed)
        self.assertTrue(responses[-1].closed)
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _ in session.calls],
            ["secret", "chat", "secret", "chat"],
        )
        chat_tokens = [
            kwargs["params"]["token"]
            for url, kwargs in session.calls
            if url.endswith("/chat")
        ]
        self.assertEqual(chat_tokens, ["stale-token", "fresh-token"])

    def test_real_client_path_classifies_second_401_and_stops(self) -> None:
        from tests.test_client import FakeResponse
        from translation_platform.models import ErrorType, FailureRecord, RecordStatus
        from translation_platform.token import TokenRefreshExhausted

        session, client = self.make_client(
            [
                FakeResponse(payload={"token": "stale-token"}),
                FakeResponse(status_code=401, status_error=RuntimeError("synthetic 401")),
                FakeResponse(payload={"token": "still-invalid-token"}),
                FakeResponse(status_code=401, status_error=RuntimeError("synthetic 401")),
            ]
        )

        with self.assertRaises(TokenRefreshExhausted) as caught:
            client.post_translation({"input": "synthetic text"})

        self.assertEqual(caught.exception.error_type, ErrorType.SIGNATURE_TOKEN)
        self.assertEqual(caught.exception.operation_attempts, 2)
        self.assertEqual(len(session.calls), 4)
        self.assertTrue(session.responses is not None)
        failure = FailureRecord(
            run_id="synthetic-run",
            input_summary="synthetic-summary",
            from_lang="zh-CHS",
            to_lang="en",
            status=RecordStatus.FAILURE,
            attempts=caught.exception.operation_attempts,
            proxy_id_summary=None,
            latency_ms=1.0,
            error_type=caught.exception.error_type,
            error_message=str(caught.exception),
        )
        self.assertEqual(failure.error_type, ErrorType.SIGNATURE_TOKEN)

    def test_sse_parse_failure_still_closes_streaming_response(self) -> None:
        from tests.test_client import FakeResponse
        from translation_platform.sse import SseJsonError

        streaming_response = FakeResponse(
            stream_lines=[b"data: {invalid}", b""]
        )
        _, client = self.make_client(
            [
                FakeResponse(payload={"token": "synthetic-token"}),
                streaming_response,
            ]
        )

        with self.assertRaises(SseJsonError):
            client.post_translation({"input": "synthetic text"})

        self.assertTrue(streaming_response.closed)

    def test_token_provider_supports_get_and_nested_token_path(self) -> None:
        from tests.test_client import FakeResponse, make_test_rate_limiter
        from translation_platform.client import JsonTokenProvider, SessionHttpClient
        from translation_platform.protocol import EndpointKind, SignedRequestBuilder
        from translation_platform.signer import JsSigner

        session = SequenceSession(
            [FakeResponse(payload={"data": {"credential": "fixture-auth-value"}})]
        )
        transport = SessionHttpClient(
            session=session,
            rate_limiter=make_test_rate_limiter(),
        )
        provider = JsonTokenProvider(
            transport=transport,
            request_builder=SignedRequestBuilder(
                JsSigner(clock=lambda: 1700000000123)
            ),
            profile=self.profile(EndpointKind.SECRET),
            secret_url="https://translation.invalid/secret",
            yduuid="fixture-device",
            request_method="GET",
            token_path=("data", "credential"),
        )

        self.assertEqual(provider(), "fixture-auth-value")
        self.assertEqual(len(session.calls), 1)
        self.assertIn("params", session.calls[0][1])


if __name__ == "__main__":
    unittest.main()
