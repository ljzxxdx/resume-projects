from __future__ import annotations

import threading
import unittest


class VirtualClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.value += delay


class GlobalRateLimiterTests(unittest.TestCase):
    def test_all_callers_share_one_minimum_interval_schedule(self) -> None:
        from translation_platform.pacing import GlobalRateLimiter

        clock = VirtualClock()
        limiter = GlobalRateLimiter(
            min_interval_seconds=1.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        acquired = [limiter.wait() for _ in range(4)]

        self.assertEqual(acquired, [0.0, 1.0, 2.0, 3.0])
        self.assertEqual(clock.sleeps, [1.0, 1.0, 1.0])

    def test_two_worker_queue_still_uses_global_request_spacing(self) -> None:
        from translation_platform.pacing import GlobalRateLimiter, TaskQueueRunner

        clock = VirtualClock()
        limiter = GlobalRateLimiter(
            min_interval_seconds=1.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        acquired = []
        acquired_lock = threading.Lock()

        def handler(item: int) -> int:
            request_time = limiter.wait()
            with acquired_lock:
                acquired.append(request_time)
            return item * 2

        results = TaskQueueRunner(workers=2, handler=handler).run(range(6))

        self.assertEqual([result.value for result in results], [0, 2, 4, 6, 8, 10])
        ordered_times = sorted(acquired)
        self.assertEqual(ordered_times, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])

    def test_invalid_interval_and_clock_values_are_rejected(self) -> None:
        from translation_platform.errors import ConfigurationFailure
        from translation_platform.pacing import GlobalRateLimiter

        for value in (0, -1, True, float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationFailure):
                    GlobalRateLimiter(min_interval_seconds=value)

        limiter = GlobalRateLimiter(
            min_interval_seconds=1.0,
            monotonic=lambda: -1.0,
            sleep=lambda delay: None,
        )
        with self.assertRaises(ConfigurationFailure):
            limiter.wait()

    def test_concurrent_callers_compete_for_distinct_time_slots(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        from translation_platform.pacing import GlobalRateLimiter

        clock = VirtualClock()
        limiter = GlobalRateLimiter(
            min_interval_seconds=1.0,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        barrier = threading.Barrier(3)

        def wait_for_slot() -> float:
            barrier.wait()
            return limiter.wait()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(wait_for_slot) for _ in range(2)]
            barrier.wait()
            acquired = sorted(future.result() for future in futures)

        self.assertEqual(acquired, [0.0, 1.0])

    def test_sleep_must_not_return_before_the_reserved_slot(self) -> None:
        from translation_platform.errors import ConfigurationFailure
        from translation_platform.pacing import GlobalRateLimiter

        limiter = GlobalRateLimiter(
            min_interval_seconds=1.0,
            monotonic=lambda: 0.0,
            sleep=lambda delay: None,
        )
        self.assertEqual(limiter.wait(), 0.0)
        with self.assertRaisesRegex(ConfigurationFailure, "reserved request slot"):
            limiter.wait()

    def test_runtime_config_drives_real_posts_and_worker_limit(self) -> None:
        from translation_platform.client import RequestRuntime
        from translation_platform.config import RuntimeConfig

        clock = VirtualClock()

        class TimedSession:
            def __init__(self) -> None:
                from tests.test_client import FakeResponse

                self.response = FakeResponse()
                self.timestamps = []
                self.headers = {}
                self.proxies = {}
                self.trust_env = True
                self.closed = False

            def post(self, url, **kwargs):
                self.timestamps.append(clock.monotonic())
                return self.response

            def close(self) -> None:
                self.closed = True

        config = RuntimeConfig(
            from_lang="zh-CHS",
            to_lang="en",
            min_interval_seconds=2.0,
            workers=2,
        )
        runtime = RequestRuntime(
            config,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        session = TimedSession()
        transport = runtime.create_http_client(session=session)

        for _ in range(3):
            transport.post(
                "https://translation.invalid/chat",
                params={"input": "synthetic text"},
                stream=True,
            )

        thread_names = runtime.run(
            range(8),
            lambda item: threading.current_thread().name,
        )

        self.assertEqual(session.timestamps, [0.0, 2.0, 4.0])
        self.assertEqual(len(thread_names), 8)
        self.assertLessEqual(
            len({result.value for result in thread_names}),
            config.workers,
        )


class TaskQueueRunnerTests(unittest.TestCase):
    def test_every_item_is_processed_once_and_results_keep_input_order(self) -> None:
        from translation_platform.pacing import TaskQueueRunner

        calls = []
        calls_lock = threading.Lock()

        def handler(item: int) -> int:
            with calls_lock:
                calls.append(item)
            return item + 10

        results = TaskQueueRunner(workers=2, handler=handler).run(range(20))

        self.assertEqual(sorted(calls), list(range(20)))
        self.assertEqual([result.index for result in results], list(range(20)))
        self.assertEqual([result.value for result in results], list(range(10, 30)))
        self.assertTrue(all(result.succeeded for result in results))

    def test_handler_failure_does_not_block_remaining_tasks_or_shutdown(self) -> None:
        from translation_platform.pacing import TaskQueueRunner

        def handler(item: int) -> int:
            if item == 2:
                raise ValueError("synthetic task failure")
            return item

        results = TaskQueueRunner(workers=2, handler=handler).run(range(5))

        self.assertEqual(len(results), 5)
        self.assertEqual([result.index for result in results], list(range(5)))
        self.assertFalse(results[2].succeeded)
        self.assertIsInstance(results[2].error, ValueError)
        self.assertTrue(results[4].succeeded)

    def test_empty_input_terminates_and_worker_count_is_bounded(self) -> None:
        from translation_platform.errors import ConfigurationFailure
        from translation_platform.pacing import TaskQueueRunner

        self.assertEqual(TaskQueueRunner(workers=1, handler=lambda item: item).run([]), ())
        for workers in (0, 3, True):
            with self.subTest(workers=workers):
                with self.assertRaises(ConfigurationFailure):
                    TaskQueueRunner(workers=workers, handler=lambda item: item)

    def test_worker_interrupt_cancels_drains_and_reraises_without_deadlock(self) -> None:
        from translation_platform.pacing import TaskQueueRunner

        calls = []
        interruption = KeyboardInterrupt()

        def handler(item: int) -> int:
            calls.append(item)
            if item == 2:
                raise interruption
            return item

        with self.assertRaises(KeyboardInterrupt) as caught:
            TaskQueueRunner(workers=1, handler=handler).run(range(100))

        self.assertIs(caught.exception, interruption)
        self.assertEqual(calls, [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
