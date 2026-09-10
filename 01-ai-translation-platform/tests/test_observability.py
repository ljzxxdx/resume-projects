"""批量运行摘要的字段闭合、统计口径与原子落盘测试。"""

from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional
from unittest.mock import patch

from translation_platform.batch import BatchRunResult, BatchTaskOutcome
from translation_platform.batch_input import CellPosition
from translation_platform.cache import BatchTask
from translation_platform.checkpoint import CheckpointRecord, CheckpointStatus
from translation_platform.errors import ConfigurationFailure, ErrorType


class ObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.started_at = datetime(2026, 8, 22, 9, 30, tzinfo=timezone.utc)
        self.finished_at = datetime(
            2026,
            8,
            22,
            17,
            30,
            1,
            250000,
            tzinfo=timezone(timedelta(hours=8)),
        )

    def observability(self):
        """把缺失模块转换为明确的 RED 断言，而不是导入错误。"""

        try:
            return importlib.import_module("translation_platform.observability")
        except ModuleNotFoundError as exc:
            self.fail("尚未实现 translation_platform.observability: " + str(exc))

    @staticmethod
    def task(cache_key: str, text: str, position_count: int) -> BatchTask:
        return BatchTask(
            cache_key=cache_key,
            source_lang="zh-CHS",
            target_lang="en",
            normalized_text=text,
            positions=tuple(
                CellPosition(index + 2, 1, "title")
                for index in range(position_count)
            ),
        )

    @classmethod
    def outcome(
        cls,
        cache_key: str,
        *,
        status: CheckpointStatus,
        attempts: int,
        latency_ms: float,
        position_count: int = 1,
        error_type: Optional[ErrorType] = None,
        source_text: str = "synthetic source",
        translated_text: str = "synthetic translation",
        error_message: Optional[str] = None,
    ) -> BatchTaskOutcome:
        task = cls.task(cache_key, source_text, position_count)
        record = CheckpointRecord(
            cache_key=cache_key,
            status=status,
            attempts=attempts,
            translated_text=(
                translated_text if status is CheckpointStatus.SUCCESS else None
            ),
            error_type=error_type,
            latency_ms=latency_ms,
        )
        return BatchTaskOutcome(
            task=task,
            record=record,
            error_message=error_message,
        )

    def mixed_result(self) -> BatchRunResult:
        outcomes = (
            self.outcome(
                "executed-success",
                status=CheckpointStatus.SUCCESS,
                attempts=1,
                latency_ms=10.0,
                position_count=2,
                source_text="PRIVATE_SOURCE_TEXT",
                translated_text="PRIVATE_TRANSLATION_TEXT",
            ),
            self.outcome(
                "executed-timeout",
                status=CheckpointStatus.FAILURE,
                attempts=3,
                latency_ms=20.0,
                error_type=ErrorType.TIMEOUT,
                error_message="proxy://PRIVATE_PROXY_VALUE",
            ),
            self.outcome(
                "executed-server-error",
                status=CheckpointStatus.FAILURE,
                attempts=2,
                latency_ms=40.0,
                position_count=2,
                error_type=ErrorType.HTTP_5XX,
                error_message=r"D:\private\absolute\input.xlsx",
            ),
            self.outcome(
                "cached-success",
                status=CheckpointStatus.SUCCESS,
                attempts=5,
                latency_ms=900.0,
            ),
            self.outcome(
                "cached-timeout",
                status=CheckpointStatus.FAILURE,
                attempts=4,
                latency_ms=800.0,
                error_type=ErrorType.TIMEOUT,
            ),
        )
        return BatchRunResult(
            outcomes=outcomes,
            replacements={},
            cache_hits=2,
            request_count=3,
            executed_cache_keys=(
                "executed-success",
                "executed-timeout",
                "executed-server-error",
            ),
        )

    def build_mixed_summary(self):
        return self.observability().build_run_summary(
            run_id="synthetic-run-42",
            result=self.mixed_result(),
            started_at=self.started_at,
            finished_at=self.finished_at,
        )

    @classmethod
    def json_strings(cls, value: object) -> Iterator[str]:
        """递归提取 JSON 中的真实文本值，避免转义掩盖泄漏。"""

        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for key, nested_value in value.items():
                yield str(key)
                yield from cls.json_strings(nested_value)
        elif isinstance(value, list):
            for nested_value in value:
                yield from cls.json_strings(nested_value)

    def test_mixed_summary_uses_final_outcomes_but_only_executed_tasks_for_timing(
        self,
    ) -> None:
        summary = self.build_mixed_summary()

        self.assertEqual(summary.run_id, "synthetic-run-42")
        self.assertEqual(summary.input_cells, 7)
        self.assertEqual(summary.unique_texts, 5)
        self.assertEqual(summary.successes, 2)
        self.assertEqual(summary.failures, 3)
        self.assertEqual(summary.cache_hits, 2)
        self.assertEqual(summary.retries, 3)
        self.assertEqual(
            dict(summary.error_counts),
            {"timeout": 2, "http_5xx": 1},
        )
        self.assertEqual(summary.average_latency_ms, 70.0 / 3.0)
        self.assertEqual(summary.p95_latency_ms, 40.0)
        self.assertEqual(summary.started_at, "2026-08-22T09:30:00+00:00")
        self.assertEqual(summary.finished_at, "2026-08-22T17:30:01.250000+08:00")

        self.assertEqual(summary.successes + summary.failures, summary.unique_texts)
        self.assertEqual(sum(summary.error_counts.values()), summary.failures)
        self.assertEqual(
            self.mixed_result().request_count + summary.cache_hits,
            summary.unique_texts,
        )

    def test_p95_uses_nearest_rank_for_twenty_hand_checked_latencies(self) -> None:
        outcomes = tuple(
            self.outcome(
                "key-" + str(index),
                status=CheckpointStatus.SUCCESS,
                attempts=1,
                latency_ms=float(index),
            )
            for index in range(1, 21)
        )
        result = BatchRunResult(
            outcomes=outcomes,
            replacements={},
            cache_hits=0,
            request_count=20,
            executed_cache_keys=tuple("key-" + str(index) for index in range(1, 21)),
        )

        summary = self.observability().build_run_summary(
            "nearest-rank-run",
            result,
            self.started_at,
            self.finished_at,
        )

        self.assertEqual(summary.average_latency_ms, 10.5)
        self.assertEqual(summary.p95_latency_ms, 19.0)

    def test_all_cached_run_has_zero_retries_and_zero_latency(self) -> None:
        outcomes = (
            self.outcome(
                "cached-ok",
                status=CheckpointStatus.SUCCESS,
                attempts=4,
                latency_ms=500.0,
            ),
            self.outcome(
                "cached-failure",
                status=CheckpointStatus.FAILURE,
                attempts=6,
                latency_ms=700.0,
                error_type=ErrorType.RATE_LIMIT,
            ),
        )
        result = BatchRunResult(
            outcomes=outcomes,
            replacements={},
            cache_hits=2,
            request_count=0,
            executed_cache_keys=(),
        )

        summary = self.observability().build_run_summary(
            "cached-run",
            result,
            self.started_at,
            self.finished_at,
        )

        self.assertEqual(summary.successes, 1)
        self.assertEqual(summary.failures, 1)
        self.assertEqual(dict(summary.error_counts), {"rate_limit": 1})
        self.assertEqual(summary.retries, 0)
        self.assertEqual(summary.average_latency_ms, 0.0)
        self.assertEqual(summary.p95_latency_ms, 0.0)

    def test_summary_rejects_non_closed_result_counts(self) -> None:
        valid_outcome = self.outcome(
            "only-key",
            status=CheckpointStatus.SUCCESS,
            attempts=1,
            latency_ms=1.0,
        )
        invalid_result = BatchRunResult(
            outcomes=(valid_outcome,),
            replacements={},
            cache_hits=0,
            request_count=0,
            executed_cache_keys=(),
        )

        with self.assertRaises(ConfigurationFailure):
            self.observability().build_run_summary(
                "invalid-run",
                invalid_result,
                self.started_at,
                self.finished_at,
            )

    def test_batch_run_result_rejects_invalid_execution_key_sets(self) -> None:
        first = self.outcome(
            "first-key",
            status=CheckpointStatus.SUCCESS,
            attempts=1,
            latency_ms=1.0,
        )
        second = self.outcome(
            "second-key",
            status=CheckpointStatus.SUCCESS,
            attempts=1,
            latency_ms=2.0,
        )
        invalid_arguments = (
            ((first, second), 0, 2, ("first-key", "first-key")),
            ((first,), 0, 1, ("ghost-key",)),
            ((first,), 0, 1, ()),
        )

        for outcomes, cache_hits, request_count, executed_keys in invalid_arguments:
            with self.subTest(executed_keys=executed_keys):
                with self.assertRaises(ConfigurationFailure):
                    BatchRunResult(
                        outcomes=outcomes,
                        replacements={},
                        cache_hits=cache_hits,
                        request_count=request_count,
                        executed_cache_keys=executed_keys,
                    )

    def test_batch_run_result_rejects_invalid_request_and_cache_counters(self) -> None:
        invalid_values = (False, -1, 0.0)

        for invalid_request_count in invalid_values:
            with self.subTest(
                field="request_count",
                value=invalid_request_count,
            ):
                with self.assertRaises(ConfigurationFailure):
                    BatchRunResult(
                        outcomes=(),
                        replacements={},
                        cache_hits=0,
                        request_count=invalid_request_count,
                        executed_cache_keys=(),
                    )

        for invalid_cache_hits in invalid_values:
            with self.subTest(field="cache_hits", value=invalid_cache_hits):
                with self.assertRaises(ConfigurationFailure):
                    BatchRunResult(
                        outcomes=(),
                        replacements={},
                        cache_hits=invalid_cache_hits,
                        request_count=0,
                        executed_cache_keys=(),
                    )

    def test_summary_rejects_naive_or_reversed_timestamps(self) -> None:
        result = self.mixed_result()
        invalid_pairs = (
            (self.started_at.replace(tzinfo=None), self.finished_at),
            (self.started_at, self.finished_at.replace(tzinfo=None)),
            (self.finished_at, self.started_at),
        )

        for started_at, finished_at in invalid_pairs:
            with self.subTest(started_at=started_at, finished_at=finished_at):
                with self.assertRaises(ConfigurationFailure):
                    self.observability().build_run_summary(
                        "invalid-time-run",
                        result,
                        started_at,
                        finished_at,
                    )

    def test_summary_is_deeply_read_only_and_has_only_fixed_fields(self) -> None:
        summary = self.build_mixed_summary()

        self.assertEqual(
            tuple(field.name for field in fields(summary)),
            (
                "run_id",
                "input_cells",
                "unique_texts",
                "successes",
                "failures",
                "cache_hits",
                "retries",
                "error_counts",
                "started_at",
                "finished_at",
                "average_latency_ms",
                "p95_latency_ms",
            ),
        )
        with self.assertRaises(FrozenInstanceError):
            summary.failures = 99
        with self.assertRaises(TypeError):
            summary.error_counts["timeout"] = 99

    def test_summary_rejects_unsafe_run_ids(self) -> None:
        unsafe_run_ids = (
            r"C:\private\run_summary.json",
            "/srv/private/run_summary.json",
            "https://example.test/private-run",
            "proxy://user:secret@10.0.0.1:8080",
            "user:password@example.test",
            "run id with spaces",
            "é-run",
            "r" * 129,
        )

        for run_id in unsafe_run_ids:
            with self.subTest(run_id=run_id):
                with self.assertRaises(ConfigurationFailure):
                    self.observability().BatchRunSummary(
                        run_id=run_id,
                        input_cells=0,
                        unique_texts=0,
                        successes=0,
                        failures=0,
                        cache_hits=0,
                        retries=0,
                        error_counts={},
                        started_at="2026-08-22T00:00:00+00:00",
                        finished_at="2026-08-22T00:00:00+00:00",
                        average_latency_ms=0.0,
                        p95_latency_ms=0.0,
                    )

    def test_summary_rejects_error_count_keys_outside_error_type(self) -> None:
        for unsafe_key in ("not_an_error", "proxy://user:secret@host"):
            with self.subTest(unsafe_key=unsafe_key):
                with self.assertRaises(ConfigurationFailure):
                    self.observability().BatchRunSummary(
                        run_id="safe-run",
                        input_cells=1,
                        unique_texts=1,
                        successes=0,
                        failures=1,
                        cache_hits=0,
                        retries=0,
                        error_counts={unsafe_key: 1},
                        started_at="2026-08-22T00:00:00+00:00",
                        finished_at="2026-08-22T00:00:01+00:00",
                        average_latency_ms=1.0,
                        p95_latency_ms=1.0,
                    )

    def test_write_run_summary_atomically_writes_only_summary_fields(self) -> None:
        observability = self.observability()
        summary = self.build_mixed_summary()
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "run_summary.json"

            observability.write_run_summary(path, summary)

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(payload), {field.name for field in fields(summary)})
            self.assertEqual(payload["error_counts"], {"timeout": 2, "http_5xx": 1})
            summary_strings = tuple(self.json_strings(payload))
            for forbidden in (
                "PRIVATE_SOURCE_TEXT",
                "PRIVATE_TRANSLATION_TEXT",
                "PRIVATE_PROXY_VALUE",
                r"D:\private\absolute\input.xlsx",
            ):
                self.assertTrue(
                    all(forbidden not in value for value in summary_strings),
                    forbidden,
                )
            self.assertEqual(list(path.parent.glob(".run_summary.json.*.tmp")), [])

    def test_failed_atomic_replace_keeps_previous_file_and_removes_temp_file(self) -> None:
        observability = self.observability()
        summary = self.build_mixed_summary()
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "run_summary.json"
            original_bytes = b'{"old":"summary"}\n'
            path.write_bytes(original_bytes)

            with patch(
                "translation_platform.observability.os.replace",
                side_effect=OSError("synthetic replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    observability.write_run_summary(path, summary)

            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertEqual(list(path.parent.glob(".run_summary.json.*.tmp")), [])

    def test_write_revalidates_identifiers_before_replacing_existing_file(self) -> None:
        observability = self.observability()
        unsafe_mutations = (
            ("run_id", "https://user:secret@example.test/private"),
            ("error_counts", {"proxy://user:secret@host": 3}),
        )

        for field_name, unsafe_value in unsafe_mutations:
            with self.subTest(field_name=field_name):
                summary = self.build_mixed_summary()
                object.__setattr__(summary, field_name, unsafe_value)
                with tempfile.TemporaryDirectory() as temporary_directory:
                    path = Path(temporary_directory) / "run_summary.json"
                    original_bytes = b'{"old":"summary"}\n'
                    path.write_bytes(original_bytes)

                    with self.assertRaises(ConfigurationFailure):
                        observability.write_run_summary(path, summary)

                    self.assertEqual(path.read_bytes(), original_bytes)
                    self.assertEqual(
                        list(path.parent.glob(".run_summary.json.*.tmp")),
                        [],
                    )


if __name__ == "__main__":
    unittest.main()
