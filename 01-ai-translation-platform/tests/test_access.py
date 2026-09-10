from __future__ import annotations

import unittest
from datetime import datetime, timezone

import requests

from tests.test_client import FakeResponse, RecordingSession, make_test_rate_limiter


class RetryAfterTests(unittest.TestCase):
    def test_session_preserves_retry_after_header_on_429(self) -> None:
        from translation_platform.client import SessionHttpClient
        from translation_platform.errors import RateLimitError

        response = FakeResponse(
            status_code=429,
            status_error=requests.HTTPError("synthetic 429"),
            headers={"Retry-After": "12"},
        )
        client = SessionHttpClient(
            session=RecordingSession(response=response),
            rate_limiter=make_test_rate_limiter(),
        )

        with self.assertRaises(RateLimitError) as caught:
            client.post("https://translation.invalid/chat", params={})

        self.assertEqual(caught.exception.retry_after_raw, "12")

    def test_delta_seconds_and_http_date_are_parsed_deterministically(self) -> None:
        from translation_platform.access import parse_retry_after
        from translation_platform.errors import ConfigurationFailure

        now = datetime(2026, 8, 17, 13, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_retry_after("12", now=now), 12.0)
        self.assertEqual(
            parse_retry_after("Mon, 17 Aug 2026 13:00:30 GMT", now=now),
            30.0,
        )
        self.assertIsNone(parse_retry_after("invalid", now=now))
        with self.assertRaises(ConfigurationFailure):
            parse_retry_after("12", now="invalid clock")


class AccessFallbackTests(unittest.TestCase):
    def controller(self, *, enabled=False, switches=0, switch_proxy=None):
        from translation_platform.access import AccessFallbackController, AccessPolicy

        return AccessFallbackController(
            policy=AccessPolicy(
                proxy_fallback_enabled=enabled,
                max_proxy_switches=switches,
            ),
            switch_proxy=(switch_proxy if switch_proxy is not None else lambda error: True),
            now=lambda: datetime(2026, 8, 17, 13, 0, 0, tzinfo=timezone.utc),
        )

    def test_retry_after_defers_429_before_any_proxy_switch(self) -> None:
        from translation_platform.errors import ErrorType, RateLimitError

        switch_calls = []
        controller = self.controller(
            enabled=True,
            switches=2,
            switch_proxy=lambda error: switch_calls.append(error) or True,
        )
        outcome = controller.execute(
            lambda: (_ for _ in ()).throw(
                RateLimitError(
                    "synthetic 429",
                    status_code=429,
                    retry_after_raw="15",
                )
            )
        )

        self.assertFalse(outcome.succeeded)
        self.assertTrue(outcome.failure.deferred)
        self.assertEqual(outcome.failure.retry_after_seconds, 15.0)
        self.assertEqual(outcome.failure.error_type, ErrorType.RATE_LIMIT)
        self.assertEqual(outcome.failure.proxy_switches, 0)
        self.assertEqual(switch_calls, [])

    def test_proxy_fallback_is_disabled_by_default_for_401_and_403(self) -> None:
        from translation_platform.errors import AccessRestrictionError
        from translation_platform.token import AuthenticationFailure

        cases = (
            AuthenticationFailure("synthetic auth", status_code=401),
            AccessRestrictionError("synthetic access", status_code=403),
        )
        for error in cases:
            with self.subTest(status_code=error.status_code):
                calls = []
                controller = self.controller()
                outcome = controller.execute(
                    lambda: calls.append(1)
                    or (_ for _ in ()).throw(error)
                )
                self.assertEqual(len(calls), 1)
                self.assertEqual(outcome.failure.proxy_switches, 0)
                self.assertEqual(outcome.failure.status_code, error.status_code)

    def test_enabled_proxy_fallback_can_recover_after_one_403_switch(self) -> None:
        from translation_platform.errors import AccessRestrictionError

        switch_calls = []
        operation_calls = []
        controller = self.controller(
            enabled=True,
            switches=2,
            switch_proxy=lambda error: switch_calls.append(error.status_code) or True,
        )

        def operation():
            operation_calls.append(1)
            if len(operation_calls) == 1:
                raise AccessRestrictionError("synthetic 403", status_code=403)
            return "synthetic success"

        outcome = controller.execute(operation)

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.value, "synthetic success")
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(outcome.proxy_switches, 1)
        self.assertEqual(switch_calls, [403])

    def test_proxy_switch_exhaustion_preserves_original_error_classification(self) -> None:
        from translation_platform.errors import ErrorType, RateLimitError

        controller = self.controller(enabled=True, switches=2)
        calls = []

        def operation():
            calls.append(1)
            raise RateLimitError("synthetic 429", status_code=429)

        outcome = controller.execute(operation)

        self.assertEqual(len(calls), 3)
        self.assertEqual(outcome.failure.error_type, ErrorType.RATE_LIMIT)
        self.assertEqual(outcome.failure.status_code, 429)
        self.assertEqual(outcome.failure.proxy_switches, 2)
        self.assertEqual(outcome.failure.attempts, 3)

    def test_other_http_errors_never_trigger_proxy_switch(self) -> None:
        from translation_platform.errors import Http4xxError, Http5xxError

        switch_calls = []
        controller = self.controller(
            enabled=True,
            switches=2,
            switch_proxy=lambda error: switch_calls.append(error) or True,
        )
        for error in (
            Http4xxError("synthetic 400", status_code=400),
            Http5xxError("synthetic 503", status_code=503),
        ):
            with self.subTest(error=type(error).__name__):
                outcome = controller.execute(
                    lambda error=error: (_ for _ in ()).throw(error)
                )
                self.assertEqual(outcome.attempts, 1)
                self.assertEqual(outcome.failure.proxy_switches, 0)
        self.assertEqual(switch_calls, [])

    def test_production_client_defers_real_429_without_proxy_switch(self) -> None:
        from tests.test_token import AuthenticatedClientIntegrationTests
        from translation_platform.access import AccessFallbackController, AccessPolicy
        from translation_platform.client import AccessControlledTranslationClient

        helper = AuthenticatedClientIntegrationTests()
        session, authenticated = helper.make_client(
            [
                FakeResponse(payload={"token": "synthetic-token"}),
                FakeResponse(
                    status_code=429,
                    status_error=requests.HTTPError("synthetic 429"),
                    headers={"Retry-After": "20"},
                ),
            ]
        )
        switch_calls = []
        controller = AccessFallbackController(
            policy=AccessPolicy(proxy_fallback_enabled=True, max_proxy_switches=1),
            switch_proxy=lambda error: switch_calls.append(error) or True,
            now=lambda: datetime(2026, 8, 17, 13, 0, 0, tzinfo=timezone.utc),
        )
        client = AccessControlledTranslationClient(authenticated, controller)

        outcome = client.post_translation({"input": "synthetic text"})

        self.assertTrue(outcome.failure.deferred)
        self.assertEqual(outcome.failure.retry_after_seconds, 20.0)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(switch_calls, [])

    def test_production_client_routes_403_to_one_proxy_switch_without_token_refresh(self) -> None:
        from tests.test_token import AuthenticatedClientIntegrationTests
        from translation_platform.access import AccessFallbackController, AccessPolicy
        from translation_platform.client import AccessControlledTranslationClient

        helper = AuthenticatedClientIntegrationTests()
        session, authenticated = helper.make_client(
            [
                FakeResponse(payload={"token": "synthetic-token"}),
                FakeResponse(
                    status_code=403,
                    status_error=requests.HTTPError("synthetic 403"),
                ),
                FakeResponse(
                    stream_lines=[
                        b'data: {"content":"synthetic success"}',
                        b"",
                        b"data: [DONE]",
                    ]
                ),
            ]
        )
        switches = []
        controller = AccessFallbackController(
            policy=AccessPolicy(proxy_fallback_enabled=True, max_proxy_switches=1),
            switch_proxy=lambda error: switches.append(error.status_code) or True,
        )
        client = AccessControlledTranslationClient(authenticated, controller)

        outcome = client.post_translation({"input": "synthetic text"})

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.value.content, "synthetic success")
        self.assertEqual(switches, [403])
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for url, _ in session.calls],
            ["secret", "chat", "chat"],
        )

    def test_token_refresh_exhaustion_is_eligible_for_explicit_fallback(self) -> None:
        from translation_platform.token import TokenRefreshExhausted

        operation_calls = []
        controller = self.controller(enabled=True, switches=1)

        def operation():
            operation_calls.append(1)
            if len(operation_calls) == 1:
                raise TokenRefreshExhausted(
                    "synthetic auth exhausted",
                    operation_attempts=2,
                    status_code=401,
                )
            return "synthetic success"

        outcome = controller.execute(operation)
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.proxy_switches, 1)

    def test_proxy_fallback_configuration_is_finite_and_consistent(self) -> None:
        from translation_platform.access import AccessPolicy
        from translation_platform.errors import ConfigurationFailure

        invalid = (
            {"proxy_fallback_enabled": False, "max_proxy_switches": 1},
            {"proxy_fallback_enabled": True, "max_proxy_switches": 0},
            {"proxy_fallback_enabled": True, "max_proxy_switches": 4},
            {"proxy_fallback_enabled": True, "max_proxy_switches": True},
        )
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(ConfigurationFailure):
                    AccessPolicy(**values)


if __name__ == "__main__":
    unittest.main()
