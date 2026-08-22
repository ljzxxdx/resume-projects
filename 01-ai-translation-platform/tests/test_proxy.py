from __future__ import annotations

import unittest

import requests


ENV_NAME = "TRANSLATION_PROXY_URLS"


def make_proxy_url(index: int, authenticated: bool = False) -> str:
    """按片段构造仅用于离线测试的保留域名 URL。"""

    authority = f"proxy-{index}.invalid:{8100 + index}"
    if authenticated:
        authority = "synthetic-user:synthetic-secret@" + authority
    return "http" + "://" + authority


class ProxySettingsTests(unittest.TestCase):
    def test_proxy_is_disabled_by_default_and_ignores_private_sources(self) -> None:
        from translation_platform.proxy import ProxySettings

        settings = ProxySettings.from_sources(
            enabled=False,
            environ={ENV_NAME: make_proxy_url(1)},
            private_config={"urls": [make_proxy_url(2)]},
        )

        self.assertFalse(settings.enabled)
        self.assertEqual(settings.urls, ())
        self.assertEqual(settings.max_switches, 0)

    def test_enabled_settings_read_environment_or_private_config(self) -> None:
        from translation_platform.proxy import ProxySettings

        first = make_proxy_url(1)
        second = make_proxy_url(2)
        settings = ProxySettings.from_sources(
            enabled=True,
            environ={ENV_NAME: first + "," + second},
            private_config={"urls": [second]},
            max_switches=2,
            cooldown_seconds=30,
        )

        self.assertEqual(settings.urls, (first, second))
        self.assertEqual(settings.max_switches, 2)
        self.assertEqual(settings.cooldown_seconds, 30.0)

    def test_invalid_or_missing_standard_urls_are_rejected_without_echoing(self) -> None:
        from translation_platform.errors import ConfigurationFailure
        from translation_platform.proxy import ProxySettings

        invalid_values = (
            "ftp" + "://proxy.invalid:9000",
            "http" + "://",
            make_proxy_url(1) + "/private-path",
            "http" + "://bad host:8080",
            "http" + "://proxy.invalid:0",
            "http" + "://bad\x7fhost:8080",
            "http" + "://bad\x81host:8080",
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ConfigurationFailure) as caught:
                    ProxySettings.from_sources(
                        enabled=True,
                        environ={ENV_NAME: invalid},
                        max_switches=1,
                    )
                self.assertNotIn(invalid, str(caught.exception))

        with self.assertRaisesRegex(ConfigurationFailure, "private proxy source"):
            ProxySettings.from_sources(enabled=True, environ={})

    def test_public_identity_is_an_irreversible_summary(self) -> None:
        from translation_platform.proxy import ProxySettings

        source = make_proxy_url(7, authenticated=True)
        settings = ProxySettings.from_sources(
            enabled=True,
            private_config={"urls": [source]},
            max_switches=1,
        )

        public_id = settings.public_ids[0]
        self.assertRegex(public_id, r"^proxy-[0-9a-f]{12}$")
        for sensitive_part in ("synthetic-user", "synthetic-secret", "proxy-7", "8107"):
            self.assertNotIn(sensitive_part, public_id)


class ProxyManagerTests(unittest.TestCase):
    class Clock:
        def __init__(self) -> None:
            self.value = 0.0

        def monotonic(self) -> float:
            return self.value

    class Session:
        def __init__(self) -> None:
            self.proxies = {"stale": "value"}
            self.trust_env = True

    def settings(
        self,
        *,
        enabled=True,
        count=3,
        switches=2,
        cooldown=10,
        access_fallback=False,
    ):
        from translation_platform.proxy import ProxySettings

        return ProxySettings.from_sources(
            enabled=enabled,
            private_config={
                "urls": [make_proxy_url(index) for index in range(1, count + 1)]
            },
            max_switches=switches if enabled else 0,
            cooldown_seconds=cooldown,
            access_status_fallback_enabled=access_fallback,
        )

    def test_disabled_manager_enforces_direct_connection(self) -> None:
        from translation_platform.proxy import ProxyManager

        session = self.Session()
        manager = ProxyManager(self.settings(enabled=False), session=session)

        self.assertIsNone(manager.current_proxy_id)
        self.assertEqual(session.proxies, {})
        self.assertFalse(session.trust_env)

    def test_connection_failures_cool_down_and_switch_with_a_finite_budget(self) -> None:
        from translation_platform.errors import NetworkConnectionError
        from translation_platform.proxy import ProxyManager

        clock = self.Clock()
        session = self.Session()
        manager = ProxyManager(
            self.settings(),
            session=session,
            monotonic=clock.monotonic,
        )
        first_id = manager.current_proxy_id

        self.assertTrue(
            manager.handle_connection_failure(NetworkConnectionError("synthetic"))
        )
        second_id = manager.current_proxy_id
        self.assertNotEqual(second_id, first_id)
        self.assertEqual(manager.switches, 1)
        self.assertEqual(manager.cooldown_remaining(first_id), 10.0)
        self.assertEqual(
            [event.action for event in manager.events],
            ["cooldown", "switch"],
        )
        event_text = repr(manager.events)
        for private_part in ("proxy-1.invalid", "8101"):
            self.assertNotIn(private_part, event_text)

        self.assertTrue(
            manager.handle_connection_failure(NetworkConnectionError("synthetic"))
        )
        self.assertEqual(manager.switches, 2)
        self.assertFalse(
            manager.handle_connection_failure(NetworkConnectionError("synthetic"))
        )
        self.assertEqual(manager.switches, 2)

    def test_non_connection_error_does_not_cool_or_switch(self) -> None:
        from translation_platform.errors import Http5xxError
        from translation_platform.proxy import ProxyManager

        manager = ProxyManager(self.settings(), session=self.Session())
        original_id = manager.current_proxy_id

        self.assertFalse(
            manager.handle_connection_failure(
                Http5xxError("synthetic", status_code=503)
            )
        )
        self.assertEqual(manager.current_proxy_id, original_id)
        self.assertEqual(manager.switches, 0)

    def test_runtime_loads_private_source_and_real_connection_failure_switches(self) -> None:
        from translation_platform.client import RequestRuntime
        from translation_platform.config import RuntimeConfig

        private_failure_detail = make_proxy_url(1, authenticated=True)

        class FailingSession(self.Session):
            def __init__(self) -> None:
                super().__init__()
                self.headers = {}
                self.calls = 0

            def post(self, url, **kwargs):
                self.calls += 1
                raise requests.ConnectionError(
                    "synthetic connection via " + private_failure_detail
                )

            def close(self) -> None:
                return None

        config = RuntimeConfig(
            from_lang="zh-CHS",
            to_lang="en",
            use_proxy=True,
            proxy_reference=ENV_NAME,
        )
        runtime = RequestRuntime.from_proxy_sources(
            config,
            environ={ENV_NAME: make_proxy_url(1) + "," + make_proxy_url(2)},
            max_proxy_switches=1,
        )
        session = FailingSession()
        client = runtime.create_http_client(session=session)
        first_id = client.proxy_manager.current_proxy_id

        with self.assertRaisesRegex(Exception, "connection failed") as caught:
            client.post("https://translation.invalid/chat", params={})

        self.assertEqual(session.calls, 1)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(private_failure_detail, str(caught.exception))
        self.assertNotEqual(client.proxy_manager.current_proxy_id, first_id)
        self.assertEqual(client.proxy_manager.switches, 1)

    def test_request_scope_prevents_concurrent_failure_misattribution(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        from translation_platform.errors import NetworkConnectionError
        from translation_platform.proxy import ProxyManager

        manager = ProxyManager(self.settings(switches=1), session=self.Session())
        first_started = Event()
        release_first = Event()
        second_attempted = Event()

        def fail_first_request():
            with manager.request_scope() as proxy_id:
                first_started.set()
                release_first.wait(timeout=2)
                switched = manager.handle_connection_failure(
                    NetworkConnectionError("synthetic"),
                    proxy_id=proxy_id,
                )
                return proxy_id, switched

        def observe_second_request():
            first_started.wait(timeout=2)
            second_attempted.set()
            with manager.request_scope() as proxy_id:
                return proxy_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(fail_first_request)
            second = executor.submit(observe_second_request)
            second_attempted.wait(timeout=2)
            self.assertFalse(second.done())
            release_first.set()
            failed_id, switched = first.result()
            second_id = second.result()

        self.assertTrue(switched)
        self.assertNotEqual(second_id, failed_id)
        health = {item.proxy_id: item for item in manager.health_snapshot()}
        self.assertEqual(health[failed_id].failure_count, 1)
        self.assertEqual(health[second_id].failure_count, 0)

    def test_late_success_cannot_erase_a_newer_connection_cooldown(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event, Lock

        from tests.test_pacing import VirtualClock
        from translation_platform.client import RequestRuntime
        from translation_platform.config import RuntimeConfig

        status_started = Event()
        release_status = Event()
        call_lock = Lock()

        class BlockingSuccessResponse:
            status_code = 200

            def raise_for_status(self) -> None:
                status_started.set()
                release_status.wait(timeout=2)

        class InterleavedSession(self.Session):
            def __init__(self) -> None:
                super().__init__()
                self.headers = {}
                self.calls = 0

            def post(self, url, **kwargs):
                with call_lock:
                    self.calls += 1
                    call_number = self.calls
                if call_number == 1:
                    return BlockingSuccessResponse()
                raise requests.ConnectionError("synthetic connection")

            def close(self) -> None:
                return None

        clock = VirtualClock()
        config = RuntimeConfig(
            from_lang="zh-CHS",
            to_lang="en",
            use_proxy=True,
            proxy_reference=ENV_NAME,
        )
        runtime = RequestRuntime.from_proxy_sources(
            config,
            environ={ENV_NAME: make_proxy_url(1) + "," + make_proxy_url(2)},
            max_proxy_switches=1,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        client = runtime.create_http_client(session=InterleavedSession())
        original_id = client.proxy_manager.current_proxy_id

        with ThreadPoolExecutor(max_workers=2) as executor:
            successful = executor.submit(
                client.post,
                "https://translation.invalid/chat",
                {},
            )
            status_started.wait(timeout=2)
            failed = executor.submit(
                client.post,
                "https://translation.invalid/chat",
                {},
            )
            self.assertFalse(failed.done())
            release_status.set()
            self.assertIsInstance(successful.result(), BlockingSuccessResponse)
            with self.assertRaises(Exception):
                failed.result()

        health = {
            item.proxy_id: item for item in client.proxy_manager.health_snapshot()
        }
        self.assertEqual(health[original_id].failure_count, 1)
        self.assertGreater(health[original_id].cooldown_remaining_seconds, 0)


class ProxyHealthTests(unittest.TestCase):
    Clock = ProxyManagerTests.Clock
    Session = ProxyManagerTests.Session
    settings = ProxyManagerTests.settings
    def test_access_statuses_require_a_separate_explicit_opt_in(self) -> None:
        from translation_platform.errors import AccessRestrictionError, RateLimitError
        from translation_platform.proxy import ProxyManager
        from translation_platform.token import AuthenticationFailure

        errors = (
            AuthenticationFailure("synthetic", status_code=401),
            AccessRestrictionError("synthetic", status_code=403),
            RateLimitError("synthetic", status_code=429),
        )
        for error in errors:
            with self.subTest(status_code=error.status_code):
                disabled = ProxyManager(self.settings(), session=self.Session())
                self.assertFalse(disabled.handle_failure(error))
                self.assertEqual(disabled.events, ())

                enabled = ProxyManager(
                    self.settings(access_fallback=True),
                    session=self.Session(),
                )
                first_id = enabled.current_proxy_id
                self.assertTrue(enabled.handle_failure(error))
                self.assertNotEqual(enabled.current_proxy_id, first_id)
                health = {
                    item.proxy_id: item for item in enabled.health_snapshot()
                }
                self.assertEqual(health[first_id].failure_count, 1)
                self.assertEqual(health[first_id].last_error_type, error.error_type)

    def test_other_business_4xx_never_marks_bad_or_rotates(self) -> None:
        from translation_platform.errors import Http4xxError
        from translation_platform.proxy import ProxyManager

        manager = ProxyManager(
            self.settings(access_fallback=True),
            session=self.Session(),
        )
        original_id = manager.current_proxy_id

        self.assertFalse(
            manager.handle_failure(Http4xxError("synthetic", status_code=400))
        )

        self.assertEqual(manager.current_proxy_id, original_id)
        self.assertEqual(manager.events, ())
        self.assertTrue(
            all(item.failure_count == 0 for item in manager.health_snapshot())
        )

    def test_access_rotation_retries_through_the_same_global_limiter(self) -> None:
        from tests.test_client import FakeResponse
        from tests.test_pacing import VirtualClock
        from translation_platform.access import AccessFallbackController, AccessPolicy
        from translation_platform.client import RequestRuntime
        from translation_platform.config import RuntimeConfig

        clock = VirtualClock()

        class SequenceStatusSession(self.Session):
            def __init__(self) -> None:
                super().__init__()
                self.headers = {}
                self.closed = False
                self.timestamps = []
                self.responses = iter(
                    (
                        FakeResponse(
                            status_code=403,
                            status_error=requests.HTTPError("synthetic 403"),
                        ),
                        FakeResponse(),
                    )
                )

            def post(self, url, **kwargs):
                self.timestamps.append(clock.monotonic())
                return next(self.responses)

            def close(self) -> None:
                self.closed = True

        config = RuntimeConfig(
            from_lang="zh-CHS",
            to_lang="en",
            min_interval_seconds=1.0,
            use_proxy=True,
            proxy_reference=ENV_NAME,
        )
        runtime = RequestRuntime.from_proxy_sources(
            config,
            environ={ENV_NAME: make_proxy_url(1) + "," + make_proxy_url(2)},
            max_proxy_switches=1,
            access_status_fallback_enabled=True,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        session = SequenceStatusSession()
        client = runtime.create_http_client(session=session)
        controller = AccessFallbackController(
            AccessPolicy(proxy_fallback_enabled=True, max_proxy_switches=1),
            switch_proxy=client.proxy_manager.handle_failure,
        )

        outcome = controller.execute(
            lambda: client.post(
                "https://translation.invalid/chat",
                params={"input": "synthetic"},
            )
        )

        self.assertTrue(outcome.succeeded)
        self.assertEqual(session.timestamps, [0.0, 1.0])
        self.assertEqual(client.proxy_manager.switches, 1)

    def test_concurrent_403_callbacks_keep_the_original_proxy_attribution(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier, Lock

        from tests.test_client import FakeResponse
        from tests.test_pacing import VirtualClock
        from translation_platform.access import AccessFallbackController, AccessPolicy
        from translation_platform.client import RequestRuntime
        from translation_platform.config import RuntimeConfig

        clock = VirtualClock()
        call_lock = Lock()

        class ConcurrentStatusSession(self.Session):
            def __init__(self) -> None:
                super().__init__()
                self.headers = {}
                self.used_proxy_ids = []
                self.manager = None

            def post(self, url, **kwargs):
                with call_lock:
                    self.used_proxy_ids.append(self.manager.current_proxy_id)
                return FakeResponse(
                    status_code=403,
                    status_error=requests.HTTPError("synthetic 403"),
                )

            def close(self) -> None:
                return None

        config = RuntimeConfig(
            from_lang="zh-CHS",
            to_lang="en",
            workers=2,
            use_proxy=True,
            proxy_reference=ENV_NAME,
        )
        runtime = RequestRuntime.from_proxy_sources(
            config,
            environ={ENV_NAME: make_proxy_url(1) + "," + make_proxy_url(2)},
            max_proxy_switches=1,
            access_status_fallback_enabled=True,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        session = ConcurrentStatusSession()
        client = runtime.create_http_client(session=session)
        session.manager = client.proxy_manager
        original_id = client.proxy_manager.current_proxy_id
        barrier = Barrier(2)

        def switch_without_retry(error) -> bool:
            barrier.wait()
            client.proxy_manager.handle_failure(error)
            return False

        controller = AccessFallbackController(
            AccessPolicy(proxy_fallback_enabled=True, max_proxy_switches=1),
            switch_proxy=switch_without_retry,
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    lambda _: controller.execute(
                        lambda: client.post(
                            "https://translation.invalid/chat",
                            params={"input": "synthetic"},
                        )
                    ),
                    range(2),
                )
            )

        self.assertTrue(all(not outcome.succeeded for outcome in outcomes))
        self.assertEqual(session.used_proxy_ids, [original_id, original_id])
        health = {
            item.proxy_id: item for item in client.proxy_manager.health_snapshot()
        }
        self.assertEqual(health[original_id].failure_count, 2)
        self.assertEqual(client.proxy_manager.switches, 1)
        self.assertEqual(
            health[client.proxy_manager.current_proxy_id].failure_count,
            0,
        )


if __name__ == "__main__":
    unittest.main()
