from __future__ import annotations

import unittest


class RetryExecutorTests(unittest.TestCase):
    def executor(self, **overrides):
        from translation_platform.retry import RetryExecutor, RetryPolicy

        delays = []
        policy_values = {
            "max_retries": 2,
            "base_delay_seconds": 1.0,
            "max_delay_seconds": 10.0,
            "jitter_ratio": 0.2,
        }
        policy_values.update(overrides)
        executor = RetryExecutor(
            policy=RetryPolicy(**policy_values),
            sleep=delays.append,
            random_source=lambda: 0.5,
        )
        return executor, delays

    def test_connection_failure_uses_exponential_backoff_then_succeeds(self) -> None:
        from translation_platform.errors import NetworkConnectionError

        executor, delays = self.executor()
        attempts = []

        def operation():
            attempts.append(1)
            if len(attempts) < 3:
                raise NetworkConnectionError("synthetic connection")
            return "synthetic success"

        outcome = executor.execute(operation)

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.value, "synthetic success")
        self.assertIsNone(outcome.failure)
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(delays, [1.0, 2.0])

    def test_timeout_exhaustion_returns_structured_failure(self) -> None:
        from translation_platform.errors import ErrorType, RequestTimeoutError

        executor, delays = self.executor()
        attempts = []

        def operation():
            attempts.append(1)
            raise RequestTimeoutError("synthetic timeout")

        outcome = executor.execute(operation)

        self.assertFalse(outcome.succeeded)
        self.assertIsNone(outcome.value)
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(outcome.failure.error_type, ErrorType.TIMEOUT)
        self.assertEqual(outcome.failure.attempts, 3)
        self.assertEqual(outcome.failure.retry_delays, (1.0, 2.0))
        self.assertTrue(outcome.failure.exhausted)
        self.assertEqual(delays, [1.0, 2.0])

    def test_only_selected_recoverable_5xx_statuses_are_retried(self) -> None:
        from translation_platform.errors import Http5xxError

        for status_code in (500, 502, 503, 504):
            with self.subTest(status_code=status_code):
                executor, delays = self.executor(max_retries=1)
                calls = []

                def operation():
                    calls.append(1)
                    if len(calls) == 1:
                        raise Http5xxError("synthetic", status_code=status_code)
                    return "recovered"

                outcome = executor.execute(operation)
                self.assertTrue(outcome.succeeded)
                self.assertEqual(len(calls), 2)
                self.assertEqual(delays, [1.0])

    def test_non_retryable_errors_return_immediately_without_sleeping(self) -> None:
        from translation_platform.errors import (
            ErrorType,
            Http4xxError,
            Http5xxError,
            RateLimitError,
            ResponseFormatError,
        )

        cases = (
            Http4xxError("synthetic 400", status_code=400),
            Http5xxError("synthetic 501", status_code=501),
            RateLimitError("synthetic 429", status_code=429),
            ResponseFormatError("synthetic response"),
        )
        for error in cases:
            with self.subTest(error=type(error).__name__):
                executor, delays = self.executor()
                outcome = executor.execute(lambda: (_ for _ in ()).throw(error))
                self.assertFalse(outcome.succeeded)
                self.assertEqual(outcome.attempts, 1)
                self.assertEqual(outcome.failure.error_type, error.error_type)
                self.assertFalse(outcome.failure.exhausted)
                self.assertEqual(delays, [])
        self.assertEqual(cases[2].error_type, ErrorType.RATE_LIMIT)

    def test_jitter_is_deterministic_and_never_exceeds_delay_cap(self) -> None:
        from translation_platform.errors import NetworkConnectionError
        from translation_platform.retry import RetryExecutor, RetryPolicy

        delays = []
        executor = RetryExecutor(
            policy=RetryPolicy(
                max_retries=2,
                base_delay_seconds=2.0,
                max_delay_seconds=3.0,
                jitter_ratio=0.5,
            ),
            sleep=delays.append,
            random_source=lambda: 1.0,
        )
        outcome = executor.execute(
            lambda: (_ for _ in ()).throw(NetworkConnectionError("synthetic"))
        )

        self.assertEqual(delays, [3.0, 3.0])
        self.assertEqual(outcome.failure.retry_delays, (3.0, 3.0))

    def test_policy_and_injected_random_values_are_validated(self) -> None:
        from translation_platform.errors import ConfigurationFailure, NetworkConnectionError
        from translation_platform.retry import RetryExecutor, RetryPolicy

        invalid_policies = (
            {"max_retries": -1},
            {"max_retries": True},
            {"base_delay_seconds": 0},
            {"max_delay_seconds": 0.5, "base_delay_seconds": 1.0},
            {"jitter_ratio": 1.1},
        )
        for values in invalid_policies:
            with self.subTest(values=values):
                defaults = {
                    "max_retries": 2,
                    "base_delay_seconds": 1.0,
                    "max_delay_seconds": 10.0,
                    "jitter_ratio": 0.2,
                }
                defaults.update(values)
                with self.assertRaises(ConfigurationFailure):
                    RetryPolicy(**defaults)

        executor = RetryExecutor(
            policy=RetryPolicy(max_retries=1),
            sleep=lambda delay: None,
            random_source=lambda: 2.0,
        )
        with self.assertRaises(ConfigurationFailure):
            executor.execute(
                lambda: (_ for _ in ()).throw(NetworkConnectionError("synthetic"))
            )

    def test_programming_errors_are_not_converted_to_retry_failures(self) -> None:
        executor, delays = self.executor()
        cause = ValueError("synthetic programming error")

        with self.assertRaises(ValueError) as caught:
            executor.execute(lambda: (_ for _ in ()).throw(cause))

        self.assertIs(caught.exception, cause)
        self.assertEqual(delays, [])


class RequestRuntimeRetryTests(unittest.TestCase):
    def test_runtime_config_drives_real_request_retries_through_global_pacing(self) -> None:
        import requests

        from tests.test_client import FakeResponse
        from tests.test_pacing import VirtualClock
        from translation_platform.client import RequestRuntime
        from translation_platform.config import RuntimeConfig

        clock = VirtualClock()

        class SequenceSession:
            def __init__(self) -> None:
                self.headers = {}
                self.proxies = {}
                self.trust_env = True
                self.closed = False
                self.timestamps = []
                self.results = iter(
                    (
                        requests.ConnectionError("synthetic connection"),
                        FakeResponse(
                            status_code=503,
                            status_error=requests.HTTPError("synthetic 503"),
                        ),
                        FakeResponse(),
                    )
                )

            def post(self, url, **kwargs):
                self.timestamps.append(clock.monotonic())
                result = next(self.results)
                if isinstance(result, Exception):
                    raise result
                return result

            def close(self) -> None:
                self.closed = True

        runtime = RequestRuntime(
            RuntimeConfig(
                from_lang="zh-CHS",
                to_lang="en",
                min_interval_seconds=1.0,
                max_retries=2,
            ),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        session = SequenceSession()
        client = runtime.create_http_client(session=session)

        outcome = runtime.execute_with_retry(
            lambda: client.post(
                "https://translation.invalid/chat",
                params={"input": "synthetic"},
            ),
            random_source=lambda: 0.5,
        )

        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(session.timestamps, [0.0, 1.0, 3.0])


if __name__ == "__main__":
    unittest.main()
