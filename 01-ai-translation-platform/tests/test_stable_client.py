from __future__ import annotations

import unittest

import requests

from tests.test_client import FakeResponse
from tests.test_pacing import VirtualClock
from tests.test_proxy import ENV_NAME, make_proxy_url


class SequenceSession:
    def __init__(self, responses, clock: VirtualClock) -> None:
        self.responses = iter(responses)
        self.clock = clock
        self.calls = []
        self.headers = {}
        self.proxies = {}
        self.trust_env = True
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs, self.clock.monotonic()))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


class StableTranslationClientTests(unittest.TestCase):
    def build_client(self, responses, *, use_proxy=False, max_retries=2):
        from tests.test_token import AuthenticatedClientIntegrationTests
        from translation_platform.access import AccessFallbackController, AccessPolicy
        from translation_platform.client import (
            AuthenticatedTranslationClient,
            BusinessRequestClient,
            JsonTokenProvider,
            RequestRuntime,
        )
        from translation_platform.config import RuntimeConfig
        from translation_platform.protocol import EndpointKind, SignedRequestBuilder
        from translation_platform.signer import JsSigner
        from translation_platform.sse import SseParser
        from translation_platform.token import TokenManager

        clock = VirtualClock()
        config = RuntimeConfig(
            from_lang="zh-CHS",
            to_lang="en",
            min_interval_seconds=1.0,
            max_retries=max_retries,
            use_proxy=use_proxy,
            proxy_reference=ENV_NAME if use_proxy else None,
        )
        if use_proxy:
            runtime = RequestRuntime.from_proxy_sources(
                config,
                environ={ENV_NAME: make_proxy_url(1) + "," + make_proxy_url(2)},
                max_proxy_switches=1,
                access_status_fallback_enabled=True,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )
        else:
            runtime = RequestRuntime(
                config,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            )
        session = SequenceSession(responses, clock)
        transport = runtime.create_http_client(session=session)
        profile_helper = AuthenticatedClientIntegrationTests()
        builder = SignedRequestBuilder(JsSigner(clock=lambda: 1700000000123))
        provider = JsonTokenProvider(
            transport=transport,
            request_builder=builder,
            profile=profile_helper.profile(EndpointKind.SECRET),
            secret_url="https://translation.invalid/secret",
            yduuid="synthetic-device-id",
        )
        authenticated = AuthenticatedTranslationClient(
            token_manager=TokenManager(provider),
            request_builder=builder,
            chat_profile=profile_helper.profile(EndpointKind.CHAT),
            business_client=BusinessRequestClient(
                transport=transport,
                business_url="https://translation.invalid/chat",
            ),
            yduuid="synthetic-device-id",
            sse_parser=SseParser(content_path=("content",)),
        )
        policy = AccessPolicy(
            proxy_fallback_enabled=use_proxy,
            max_proxy_switches=1 if use_proxy else 0,
        )
        controller = AccessFallbackController(
            policy,
            switch_proxy=transport.proxy_manager.handle_failure,
        )
        stable = runtime.create_stable_translation_client(
            authenticated,
            controller,
            random_source=lambda: 0.5,
        )
        return stable, session, transport

    @staticmethod
    def success_response(text="synthetic translation"):
        return FakeResponse(
            stream_lines=[
                ('data: {"content":"' + text + '"}').encode("utf-8"),
                b"",
                b"data: [DONE]",
            ]
        )

    def test_network_and_recoverable_503_retry_before_success(self) -> None:
        stable, session, _ = self.build_client(
            [
                FakeResponse(payload={"token": "synthetic-token"}),
                requests.ConnectionError("synthetic connection"),
                FakeResponse(
                    status_code=503,
                    status_error=requests.HTTPError("synthetic 503"),
                ),
                self.success_response(),
            ]
        )

        outcome = stable.post_translation({"input": "synthetic"})

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.value.content, "synthetic translation")
        self.assertEqual(outcome.request_attempts, 3)
        self.assertEqual(outcome.retry_delays, (1.0, 2.0))
        self.assertEqual(
            [timestamp for _, _, timestamp in session.calls],
            [0.0, 1.0, 2.0, 4.0],
        )

    def test_network_proxy_switch_is_included_in_structured_totals(self) -> None:
        stable, _, transport = self.build_client(
            [
                FakeResponse(payload={"token": "synthetic-token"}),
                requests.ConnectionError("synthetic connection"),
                self.success_response("recovered"),
            ],
            use_proxy=True,
        )

        outcome = stable.post_translation({"input": "synthetic"})

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.retry_proxy_switches, 1)
        self.assertEqual(outcome.access_proxy_switches, 0)
        self.assertEqual(outcome.proxy_switches, 1)
        self.assertEqual(transport.proxy_manager.switches, 1)

    def test_token_endpoint_network_failure_remains_retryable(self) -> None:
        stable, session, _ = self.build_client(
            [
                requests.ConnectionError("synthetic token connection"),
                FakeResponse(payload={"token": "synthetic-token"}),
                self.success_response("recovered"),
            ]
        )

        outcome = stable.post_translation({"input": "synthetic"})

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.request_attempts, 2)
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _, _ in session.calls],
            ["secret", "secret", "chat"],
        )

    def test_429_retry_after_is_deferred_without_retry_or_proxy_switch(self) -> None:
        stable, session, transport = self.build_client(
            [
                FakeResponse(payload={"token": "synthetic-token"}),
                FakeResponse(
                    status_code=429,
                    status_error=requests.HTTPError("synthetic 429"),
                    headers={"Retry-After": "12"},
                ),
            ],
            use_proxy=True,
        )

        outcome = stable.post_translation({"input": "synthetic"})

        self.assertFalse(outcome.succeeded)
        self.assertTrue(outcome.failure.deferred)
        self.assertEqual(outcome.failure.retry_after_seconds, 12.0)
        self.assertEqual(outcome.request_attempts, 1)
        self.assertEqual(outcome.proxy_switches, 0)
        self.assertEqual(transport.proxy_manager.switches, 0)
        self.assertEqual(len(session.calls), 2)

    def test_exhausted_401_refresh_can_use_one_proxy_fallback(self) -> None:
        stable, session, transport = self.build_client(
            [
                FakeResponse(payload={"token": "stale-token"}),
                FakeResponse(
                    status_code=401,
                    status_error=requests.HTTPError("synthetic 401"),
                ),
                FakeResponse(payload={"token": "still-invalid-token"}),
                FakeResponse(
                    status_code=401,
                    status_error=requests.HTTPError("synthetic 401"),
                ),
                FakeResponse(payload={"token": "fresh-token"}),
                self.success_response("recovered"),
            ],
            use_proxy=True,
        )

        outcome = stable.post_translation({"input": "synthetic"})

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.value.content, "recovered")
        self.assertEqual(outcome.retry_proxy_switches, 0)
        self.assertEqual(outcome.access_proxy_switches, 1)
        self.assertEqual(outcome.proxy_switches, 1)
        self.assertEqual(transport.proxy_manager.switches, 1)
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _, _ in session.calls],
            ["secret", "chat", "secret", "chat", "secret", "chat"],
        )

    def test_403_switches_proxy_without_refreshing_token(self) -> None:
        stable, session, transport = self.build_client(
            [
                FakeResponse(payload={"token": "synthetic-token"}),
                FakeResponse(
                    status_code=403,
                    status_error=requests.HTTPError("synthetic 403"),
                ),
                self.success_response("recovered"),
            ],
            use_proxy=True,
        )

        outcome = stable.post_translation({"input": "synthetic"})

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.retry_proxy_switches, 0)
        self.assertEqual(outcome.access_proxy_switches, 1)
        self.assertEqual(outcome.proxy_switches, 1)
        self.assertEqual(transport.proxy_manager.switches, 1)
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _, _ in session.calls],
            ["secret", "chat", "chat"],
        )


if __name__ == "__main__":
    unittest.main()
