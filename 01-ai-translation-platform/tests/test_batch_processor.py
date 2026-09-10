from __future__ import annotations

from dataclasses import replace
import unittest

from translation_platform.batch_input import CellPosition
from translation_platform.cache import BatchTask
from translation_platform.checkpoint import CheckpointRecord, CheckpointStatus
from translation_platform.errors import ConfigurationFailure, EmptyTranslationError, ErrorType
from translation_platform.models import FailureRecord, RecordStatus, TranslationResult


class BatchProcessorTests(unittest.TestCase):
    def task(self) -> BatchTask:
        return BatchTask(
            cache_key="cache-key-for-synthetic-text",
            source_lang="zh-CHS",
            target_lang="en",
            normalized_text="合成文本",
            positions=(
                CellPosition(row_index=2, column_index=1, column_name="标题"),
                CellPosition(row_index=4, column_index=3, column_name="备注"),
            ),
        )

    def success(self, task: BatchTask, attempts: int = 1) -> TranslationResult:
        return TranslationResult(
            run_id="synthetic-run",
            input_summary="sha256:" + task.cache_key,
            from_lang=task.source_lang,
            to_lang=task.target_lang,
            status=RecordStatus.SUCCESS,
            attempts=attempts,
            proxy_id_summary="proxy:synthetic",
            latency_ms=12.5,
            error_type=None,
            translated_text="synthetic translation",
        )

    def failure(self, task: BatchTask, attempts: int = 1) -> FailureRecord:
        return FailureRecord(
            run_id="synthetic-run",
            input_summary="sha256:" + task.cache_key,
            from_lang=task.source_lang,
            to_lang=task.target_lang,
            status=RecordStatus.FAILURE,
            attempts=attempts,
            proxy_id_summary=None,
            latency_ms=7.0,
            error_type=ErrorType.TIMEOUT,
            error_message="synthetic timeout",
        )

    def process(self, translator, max_attempts: int = 3):
        from translation_platform.batch import BatchProcessor

        return BatchProcessor(translator, max_attempts=max_attempts).process(self.task())

    def test_success_becomes_checkpoint_success_with_every_source_position(self) -> None:
        task = self.task()
        result = self.success(task, attempts=2)

        outcome = self.process(lambda received_task: result)

        self.assertEqual(outcome.task, task)
        self.assertEqual(outcome.task.positions, task.positions)
        self.assertEqual(
            outcome.record,
            CheckpointRecord(
                cache_key="cache-key-for-synthetic-text",
                status=CheckpointStatus.SUCCESS,
                attempts=2,
                translated_text="synthetic translation",
                error_type=None,
                latency_ms=12.5,
            ),
        )

    def test_structured_failure_becomes_checkpoint_failure_with_every_source_position(self) -> None:
        task = self.task()
        result = self.failure(task, attempts=2)

        outcome = self.process(lambda received_task: result)

        self.assertEqual(outcome.task, task)
        self.assertEqual(outcome.task.positions, task.positions)
        self.assertEqual(
            outcome.record,
            CheckpointRecord(
                cache_key="cache-key-for-synthetic-text",
                status=CheckpointStatus.FAILURE,
                attempts=2,
                translated_text=None,
                error_type=ErrorType.TIMEOUT,
                latency_ms=7.0,
            ),
        )

    def test_empty_translation_exception_becomes_empty_result_failure(self) -> None:
        def translator(task: BatchTask):
            raise EmptyTranslationError("synthetic empty response")

        outcome = self.process(translator)

        self.assertEqual(outcome.task.positions, self.task().positions)
        self.assertEqual(outcome.record.status, CheckpointStatus.FAILURE)
        self.assertEqual(outcome.record.attempts, 1)
        self.assertIsNone(outcome.record.translated_text)
        self.assertEqual(outcome.record.error_type, ErrorType.EMPTY_RESULT)
        self.assertEqual(outcome.record.latency_ms, 0.0)

    def test_defensively_rejected_empty_success_becomes_empty_result_failure(self) -> None:
        task = self.task()
        result = self.success(task)
        object.__setattr__(result, "translated_text", "   ")

        outcome = self.process(lambda received_task: result)

        self.assertEqual(outcome.task.positions, task.positions)
        self.assertEqual(outcome.record.status, CheckpointStatus.FAILURE)
        self.assertEqual(outcome.record.attempts, 1)
        self.assertIsNone(outcome.record.translated_text)
        self.assertEqual(outcome.record.error_type, ErrorType.EMPTY_RESULT)
        self.assertEqual(outcome.record.latency_ms, 12.5)

    def test_attempts_above_configured_limit_become_response_format_failures(self) -> None:
        for maximum in (1, 6):
            with self.subTest(maximum=maximum):
                task = self.task()
                result = self.success(task, attempts=maximum + 1)

                outcome = self.process(
                    lambda received_task: result,
                    max_attempts=maximum,
                )

                self.assertEqual(outcome.record.status, CheckpointStatus.FAILURE)
                self.assertEqual(outcome.task.positions, task.positions)
                self.assertEqual(outcome.record.attempts, maximum)
                self.assertIsNone(outcome.record.translated_text)
                self.assertEqual(outcome.record.error_type, ErrorType.RESPONSE_FORMAT)
                self.assertEqual(outcome.record.latency_ms, 12.5)

    def test_mismatched_language_direction_or_input_summary_becomes_failure(self) -> None:
        task = self.task()
        cases = (
            replace(self.success(task), from_lang="en", to_lang="zh-CHS"),
            replace(self.success(task), input_summary="sha256:another-cache-key"),
        )

        for result in cases:
            with self.subTest(result=result):
                outcome = self.process(lambda received_task: result)

                self.assertEqual(outcome.task.positions, task.positions)
                self.assertEqual(outcome.record.status, CheckpointStatus.FAILURE)
                self.assertEqual(outcome.record.attempts, 1)
                self.assertIsNone(outcome.record.translated_text)
                self.assertEqual(outcome.record.error_type, ErrorType.RESPONSE_FORMAT)
                self.assertEqual(outcome.record.latency_ms, 12.5)

    def test_processor_accepts_only_one_to_six_attempts(self) -> None:
        from translation_platform.batch import BatchProcessor

        for maximum in (0, 7, True):
            with self.subTest(maximum=maximum):
                with self.assertRaisesRegex(
                    ConfigurationFailure,
                    "max_attempts 必须在 1 到 6 之间",
                ):
                    BatchProcessor(lambda task: self.success(task), max_attempts=maximum)

    def test_processor_requires_callable_translator(self) -> None:
        from translation_platform.batch import BatchProcessor

        with self.assertRaisesRegex(
            ConfigurationFailure,
            "translator 必须是可调用对象",
        ):
            BatchProcessor(None, max_attempts=3)


if __name__ == "__main__":
    unittest.main()
