"""批量运行器的恢复、显式重试与幂等行为测试。"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Dict, Optional, Set

from translation_platform.batch_input import CellPosition
from translation_platform.cache import BatchTask, build_cache_key
from translation_platform.checkpoint import (
    CheckpointRecord,
    CheckpointStatus,
    JsonlCheckpointStore,
)
from translation_platform.errors import ConfigurationFailure, ErrorType
from translation_platform.models import (
    FailureRecord,
    RecordStatus,
    TranslationResult,
)


class CountingTranslator:
    """记录真实处理边界的调用，并返回人工合成结果。"""

    def __init__(
        self,
        failures: Optional[Set[str]] = None,
        exception_by_text: Optional[Dict[str, BaseException]] = None,
    ) -> None:
        self.failures = set() if failures is None else set(failures)
        self.exception_by_text = (
            {} if exception_by_text is None else dict(exception_by_text)
        )
        self.calls = []

    def __call__(self, task: BatchTask):
        self.calls.append(task.cache_key)
        if task.normalized_text in self.exception_by_text:
            raise self.exception_by_text[task.normalized_text]
        if task.normalized_text in self.failures:
            return FailureRecord(
                run_id="synthetic-run",
                input_summary="sha256:" + task.cache_key,
                from_lang=task.source_lang,
                to_lang=task.target_lang,
                status=RecordStatus.FAILURE,
                attempts=1,
                proxy_id_summary=None,
                latency_ms=8.0,
                error_type=ErrorType.TIMEOUT,
                error_message="synthetic timeout",
            )
        return TranslationResult(
            run_id="synthetic-run",
            input_summary="sha256:" + task.cache_key,
            from_lang=task.source_lang,
            to_lang=task.target_lang,
            status=RecordStatus.SUCCESS,
            attempts=1,
            proxy_id_summary=None,
            latency_ms=5.0,
            error_type=None,
            translated_text="translated:" + task.normalized_text,
        )


class BatchRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        checkpoint_path = Path(self.temporary_directory.name) / "checkpoint.jsonl"
        self.store = JsonlCheckpointStore(checkpoint_path)

    def task(
        self,
        text: str,
        row_index: int,
        duplicate_row_index: Optional[int] = None,
    ) -> BatchTask:
        positions = [CellPosition(row_index, 1, "title")]
        if duplicate_row_index is not None:
            positions.append(CellPosition(duplicate_row_index, 2, "description"))
        return BatchTask(
            cache_key=build_cache_key("zh-CHS", "en", text),
            source_lang="zh-CHS",
            target_lang="en",
            normalized_text=text,
            positions=tuple(positions),
        )

    def runner(self, translator: CountingTranslator):
        from translation_platform.batch import BatchProcessor, BatchRunner

        return BatchRunner(BatchProcessor(translator, max_attempts=3), self.store)

    def test_first_run_translates_one_deduplicated_task_once_and_fills_all_positions(
        self,
    ) -> None:
        task = self.task("same synthetic text", 2, duplicate_row_index=5)
        translator = CountingTranslator()

        result = self.runner(translator).run((task,))

        self.assertEqual(translator.calls, [task.cache_key])
        self.assertEqual(result.request_count, 1)
        self.assertEqual(result.cache_hits, 0)
        self.assertEqual(len(result.outcomes), 1)
        self.assertEqual(
            result.replacements,
            {
                task.positions[0]: "translated:same synthetic text",
                task.positions[1]: "translated:same synthetic text",
            },
        )
        self.assertEqual(
            self.store.load()[task.cache_key].status,
            CheckpointStatus.SUCCESS,
        )

    def test_interruption_keeps_completed_success_and_pending_task_for_resume(self) -> None:
        first = self.task("first synthetic text", 2)
        second = self.task("second synthetic text", 3)
        interrupted = CountingTranslator(
            exception_by_text={"second synthetic text": KeyboardInterrupt()}
        )

        with self.assertRaises(KeyboardInterrupt):
            self.runner(interrupted).run((first, second))

        interrupted_records = self.store.load()
        self.assertEqual(
            interrupted_records[first.cache_key].status,
            CheckpointStatus.SUCCESS,
        )
        self.assertEqual(
            interrupted_records[second.cache_key].status,
            CheckpointStatus.PENDING,
        )

        resumed = CountingTranslator()
        result = self.runner(resumed).run((first, second))

        self.assertEqual(resumed.calls, [second.cache_key])
        self.assertEqual(result.cache_hits, 1)
        self.assertEqual(result.request_count, 1)
        self.assertEqual(
            result.replacements,
            {
                first.positions[0]: "translated:first synthetic text",
                second.positions[0]: "translated:second synthetic text",
            },
        )

    def test_ordinary_exception_propagates_and_leaves_task_pending(self) -> None:
        task = self.task("exception synthetic text", 2)
        translator = CountingTranslator(
            exception_by_text={"exception synthetic text": RuntimeError("synthetic")}
        )

        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            self.runner(translator).run((task,))

        self.assertEqual(
            self.store.load()[task.cache_key].status,
            CheckpointStatus.PENDING,
        )

    def test_existing_failure_is_returned_without_an_implicit_retry(self) -> None:
        task = self.task("failed synthetic text", 2)
        failure = CheckpointRecord(
            cache_key=task.cache_key,
            status=CheckpointStatus.FAILURE,
            attempts=2,
            translated_text=None,
            error_type=ErrorType.TIMEOUT,
            latency_ms=20.0,
        )
        self.store.save({task.cache_key: failure})
        translator = CountingTranslator()

        result = self.runner(translator).run((task,))

        self.assertEqual(translator.calls, [])
        self.assertEqual(result.request_count, 0)
        self.assertEqual(result.cache_hits, 1)
        self.assertEqual(result.outcomes[0].record, failure)
        self.assertEqual(result.replacements, {})

    def test_retry_failures_rejects_non_boolean_before_checkpoint_write_or_request(
        self,
    ) -> None:
        task = self.task("invalid retry flag", 2)
        failure = CheckpointRecord(
            cache_key=task.cache_key,
            status=CheckpointStatus.FAILURE,
            attempts=2,
            translated_text=None,
            error_type=ErrorType.TIMEOUT,
            latency_ms=20.0,
        )
        self.store.save({task.cache_key: failure})
        original_bytes = self.store.path.read_bytes()
        translator = CountingTranslator()
        runner = self.runner(translator)

        for invalid_value in ("false", 1):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaisesRegex(
                    ConfigurationFailure,
                    "retry_failures 必须是布尔值",
                ):
                    runner.run((task,), retry_failures=invalid_value)

                self.assertEqual(self.store.path.read_bytes(), original_bytes)
                self.assertEqual(translator.calls, [])

    def test_retry_failures_executes_failure_and_pending_but_skips_success(self) -> None:
        success_task = self.task("saved success", 2)
        failure_task = self.task("saved failure", 3)
        pending_task = self.task("saved pending", 4)
        self.store.save(
            {
                success_task.cache_key: CheckpointRecord(
                    cache_key=success_task.cache_key,
                    status=CheckpointStatus.SUCCESS,
                    attempts=1,
                    translated_text="translated:saved success",
                    error_type=None,
                    latency_ms=5.0,
                ),
                failure_task.cache_key: CheckpointRecord(
                    cache_key=failure_task.cache_key,
                    status=CheckpointStatus.FAILURE,
                    attempts=3,
                    translated_text=None,
                    error_type=ErrorType.TIMEOUT,
                    latency_ms=30.0,
                ),
                pending_task.cache_key: CheckpointRecord(
                    cache_key=pending_task.cache_key,
                    status=CheckpointStatus.PENDING,
                    attempts=0,
                    translated_text=None,
                    error_type=None,
                    latency_ms=0.0,
                ),
            }
        )
        translator = CountingTranslator()

        result = self.runner(translator).run(
            (success_task, failure_task, pending_task),
            retry_failures=True,
        )

        self.assertEqual(
            translator.calls,
            [failure_task.cache_key, pending_task.cache_key],
        )
        self.assertEqual(result.cache_hits, 1)
        self.assertEqual(result.request_count, 2)
        self.assertEqual(
            result.executed_cache_keys,
            (failure_task.cache_key, pending_task.cache_key),
        )
        self.assertEqual(len(result.replacements), 3)
        self.assertTrue(
            all(
                record.status is CheckpointStatus.SUCCESS
                for record in self.store.load().values()
            )
        )

    def test_second_identical_run_is_all_cache_hits_without_requests(self) -> None:
        tasks = (
            self.task("first idempotent text", 2, duplicate_row_index=5),
            self.task("second idempotent text", 3),
        )
        first_translator = CountingTranslator()
        self.runner(first_translator).run(tasks)
        second_translator = CountingTranslator()

        result = self.runner(second_translator).run(tasks)

        self.assertEqual(second_translator.calls, [])
        self.assertEqual(result.cache_hits, 2)
        self.assertEqual(result.request_count, 0)
        self.assertEqual(len(result.replacements), 3)
        self.assertEqual(
            result.replacements[tasks[0].positions[1]],
            "translated:first idempotent text",
        )

    def test_duplicate_cache_keys_are_rejected_before_any_request_or_write(self) -> None:
        first = self.task("duplicate task", 2)
        second = self.task("duplicate task", 3)
        translator = CountingTranslator()

        with self.assertRaisesRegex(
            ConfigurationFailure,
            "tasks 不能包含重复 cache_key",
        ):
            self.runner(translator).run((first, second))

        self.assertEqual(translator.calls, [])
        self.assertFalse(self.store.path.exists())

    def test_run_result_is_read_only(self) -> None:
        task = self.task("immutable result", 2)
        result = self.runner(CountingTranslator()).run((task,))

        with self.assertRaises(FrozenInstanceError):
            result.request_count = 99
        with self.assertRaises(TypeError):
            result.replacements[task.positions[0]] = "changed"


if __name__ == "__main__":
    unittest.main()
