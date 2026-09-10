from __future__ import annotations

import unittest


class RecordingResource:
    def __init__(self, events, close_error=None) -> None:
        self.events = events
        self.close_error = close_error
        self.closed = False

    def close(self) -> None:
        self.events.append("close")
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class RunLifecycleTests(unittest.TestCase):
    def lifecycle(
        self,
        *,
        close_error=None,
        checkpoint_error=None,
        summary_error=None,
    ):
        from translation_platform.lifecycle import RunLifecycle

        events = []
        checkpoints = []
        summaries = []
        resource = RecordingResource(events, close_error=close_error)

        def save_checkpoint(state) -> None:
            events.append("checkpoint")
            checkpoints.append(state)
            if checkpoint_error is not None:
                raise checkpoint_error

        def write_summary(state) -> None:
            events.append("summary")
            summaries.append(state)
            if summary_error is not None:
                raise summary_error

        lifecycle = RunLifecycle(resource, save_checkpoint, write_summary)
        return lifecycle, resource, events, checkpoints, summaries

    def test_normal_end_closes_session_then_saves_checkpoint_and_summary(self) -> None:
        from translation_platform.lifecycle import RunExitReason

        lifecycle, resource, events, checkpoints, summaries = self.lifecycle()

        value = lifecycle.execute(lambda: events.append("operation") or "value")

        self.assertEqual(value, "value")
        self.assertTrue(resource.closed)
        self.assertEqual(events, ["operation", "close", "checkpoint", "summary"])
        self.assertEqual(checkpoints[0].reason, RunExitReason.NORMAL)
        self.assertEqual(summaries[0].reason, RunExitReason.NORMAL)
        self.assertEqual(summaries[0].cleanup_failures, ())

    def test_error_end_finalizes_then_reraises_the_original_exception(self) -> None:
        from translation_platform.lifecycle import RunExitReason

        lifecycle, resource, events, checkpoints, summaries = self.lifecycle()
        original = ValueError("private failure detail")

        with self.assertRaises(ValueError) as caught:
            lifecycle.execute(lambda: (_ for _ in ()).throw(original))

        self.assertIs(caught.exception, original)
        self.assertTrue(resource.closed)
        self.assertEqual(events, ["close", "checkpoint", "summary"])
        self.assertEqual(summaries[0].reason, RunExitReason.ERROR)
        self.assertEqual(summaries[0].primary_error_name, "ValueError")
        self.assertNotIn("private failure detail", repr(summaries[0]))

    def test_user_interrupt_also_closes_checkpoints_and_summarizes(self) -> None:
        from translation_platform.lifecycle import RunExitReason

        lifecycle, resource, events, checkpoints, summaries = self.lifecycle()
        interruption = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt) as caught:
            lifecycle.execute(lambda: (_ for _ in ()).throw(interruption))

        self.assertIs(caught.exception, interruption)
        self.assertTrue(resource.closed)
        self.assertEqual(events, ["close", "checkpoint", "summary"])
        self.assertEqual(checkpoints[0].reason, RunExitReason.INTERRUPTED)
        self.assertEqual(summaries[0].reason, RunExitReason.INTERRUPTED)

    def test_cleanup_failures_do_not_skip_later_steps(self) -> None:
        from translation_platform.lifecycle import LifecycleFinalizationError

        lifecycle, resource, events, checkpoints, summaries = self.lifecycle(
            close_error=RuntimeError("private close detail"),
            checkpoint_error=OSError("private checkpoint detail"),
        )

        with self.assertRaises(LifecycleFinalizationError) as caught:
            lifecycle.execute(lambda: "value")

        self.assertTrue(resource.closed)
        self.assertEqual(events, ["close", "checkpoint", "summary"])
        self.assertEqual(
            [failure.step for failure in caught.exception.failures],
            ["close_session", "save_checkpoint"],
        )
        self.assertEqual(
            [failure.error_name for failure in summaries[0].cleanup_failures],
            ["RuntimeError", "OSError"],
        )
        self.assertNotIn("private", repr(summaries[0]))

    def test_primary_error_is_not_overwritten_by_cleanup_failures(self) -> None:
        lifecycle, resource, events, checkpoints, summaries = self.lifecycle(
            close_error=RuntimeError("synthetic close"),
            checkpoint_error=OSError("synthetic checkpoint"),
        )
        original = LookupError("synthetic operation")

        with self.assertRaises(LookupError) as caught:
            lifecycle.execute(lambda: (_ for _ in ()).throw(original))

        self.assertIs(caught.exception, original)
        self.assertTrue(resource.closed)
        self.assertEqual(events, ["close", "checkpoint", "summary"])
        self.assertEqual(len(lifecycle.last_finalization.cleanup_failures), 2)

    def test_interrupt_during_checkpoint_still_writes_summary_then_reraises(self) -> None:
        from translation_platform.lifecycle import RunExitReason

        interruption = KeyboardInterrupt()
        lifecycle, resource, events, checkpoints, summaries = self.lifecycle(
            checkpoint_error=interruption,
        )

        with self.assertRaises(KeyboardInterrupt) as caught:
            lifecycle.execute(lambda: "value")

        self.assertIs(caught.exception, interruption)
        self.assertTrue(resource.closed)
        self.assertEqual(events, ["close", "checkpoint", "summary"])
        self.assertEqual(summaries[0].reason, RunExitReason.INTERRUPTED)

    def test_invalid_operation_is_an_error_exit_that_still_finalizes(self) -> None:
        from translation_platform.errors import ConfigurationFailure
        from translation_platform.lifecycle import RunExitReason

        lifecycle, resource, events, checkpoints, summaries = self.lifecycle()

        with self.assertRaises(ConfigurationFailure):
            lifecycle.execute(None)

        self.assertTrue(resource.closed)
        self.assertEqual(events, ["close", "checkpoint", "summary"])
        self.assertEqual(summaries[0].reason, RunExitReason.ERROR)

    def test_summary_writer_failure_is_reported_structurally(self) -> None:
        from translation_platform.lifecycle import LifecycleFinalizationError

        lifecycle, resource, events, checkpoints, summaries = self.lifecycle(
            summary_error=OSError("private summary detail"),
        )

        with self.assertRaises(LifecycleFinalizationError) as caught:
            lifecycle.execute(lambda: "value")

        self.assertTrue(resource.closed)
        self.assertEqual(events, ["close", "checkpoint", "summary"])
        self.assertEqual(
            [failure.step for failure in caught.exception.failures],
            ["write_summary"],
        )
        self.assertNotIn("private summary detail", str(caught.exception))


class RequestRuntimeLifecycleTests(unittest.TestCase):
    def test_request_runtime_managed_execution_owns_real_client_cleanup(self) -> None:
        from tests.test_client import RecordingSession
        from tests.test_pacing import VirtualClock
        from translation_platform.client import RequestRuntime
        from translation_platform.config import RuntimeConfig

        clock = VirtualClock()
        runtime = RequestRuntime(
            RuntimeConfig(from_lang="zh-CHS", to_lang="en"),
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )
        session = RecordingSession()
        session.proxies = {}
        client = runtime.create_http_client(session=session)
        checkpoints = []
        summaries = []

        value = runtime.execute_managed(
            client,
            operation=lambda: "synthetic result",
            save_checkpoint=checkpoints.append,
            write_summary=summaries.append,
        )

        self.assertEqual(value, "synthetic result")
        self.assertTrue(session.closed)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(len(summaries), 1)


if __name__ == "__main__":
    unittest.main()
