"""千行合成批处理离线验收入口的编排测试。"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


class SyntheticBatchValidationTests(unittest.TestCase):
    """以小规模数据验证生产批处理路径的离线验收语义。"""

    def test_small_run_recovers_and_writes_aggregate_only_evidence(self) -> None:
        """缺少任一编排环节都会破坏恢复、回填或零请求证据。"""

        from scripts.validate_synthetic_batch import run_synthetic_validation
        from translation_platform.retry import RetryExecutor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_path = root / "evidence.json"
            temporary_parent = root / "temporary"
            temporary_parent.mkdir()

            retry_executor_invocations = []
            real_execute = RetryExecutor.execute

            def recording_execute(executor, operation):
                retry_executor_invocations.append(1)
                return real_execute(executor, operation)

            with (
                patch(
                    "socket.create_connection",
                    side_effect=AssertionError("离线验收不得访问网络"),
                ),
                patch.object(RetryExecutor, "execute", new=recording_execute),
            ):
                evidence = run_synthetic_validation(
                    row_count=12,
                    evidence_path=evidence_path,
                    temporary_parent=temporary_parent,
                )

            self.assertEqual(len(retry_executor_invocations), 6)

            self.assertEqual(
                evidence["dataset"],
                {
                    "rows": 12,
                    "input_cells": 24,
                    "unique_texts": 6,
                    "deduplicated_cells": 18,
                },
            )
            self.assertEqual(evidence["evidence_type"], "offline_synthetic_mock")
            self.assertEqual(
                evidence["disclaimer"],
                {
                    "offline": True,
                    "network_accessed": False,
                    "actual_api_calls": 0,
                    "row_count_is_actual_api_call_count": False,
                },
            )
            self.assertEqual(
                evidence["fault_injection"],
                {
                    "temporary_failure": {
                        "injected": True,
                        "retry_engine": "production_retry_executor",
                        "max_retries": 1,
                        "maximum_attempts": 2,
                        "attempts_observed": 2,
                        "retries_observed": 1,
                        "retry_delays_seconds": [0.001],
                        "sleep_strategy": "record_only_no_wait",
                        "terminated": True,
                    },
                    "interruption": {
                        "injected": True,
                        "times": 1,
                        "completed_before_interruption": 3,
                        "pending_after_interruption": 3,
                    },
                },
            )
            self.assertEqual(evidence["recovery"]["resume_task_requests"], 3)
            self.assertEqual(evidence["recovery"]["resume_cache_hits"], 3)
            self.assertEqual(evidence["recovery"]["verified_positions"], 24)
            self.assertTrue(evidence["recovery"]["only_unfinished_tasks_processed"])
            self.assertTrue(evidence["recovery"]["all_positions_verified"])
            self.assertEqual(
                evidence["mock_usage"],
                {
                    "unique_task_keys": 6,
                    "batch_task_executions": 7,
                    "retry_executor_invocations": 6,
                    "request_attempts": 7,
                    "successful_unique_tasks": 6,
                    "temporary_retry_attempts": 1,
                    "interruption_attempts": 1,
                    "actual_api_calls": 0,
                },
            )
            self.assertEqual(evidence["resume_summary"]["input_cells"], 24)
            self.assertEqual(evidence["resume_summary"]["unique_texts"], 6)
            self.assertEqual(evidence["resume_summary"]["successes"], 6)
            self.assertEqual(evidence["resume_summary"]["failures"], 0)
            self.assertEqual(evidence["resume_summary"]["cache_hits"], 3)
            self.assertEqual(evidence["resume_summary"]["retries"], 1)
            self.assertEqual(evidence["resume_summary"]["error_counts"], {})
            self.assertEqual(
                evidence["second_identical_run"],
                {
                    "batch_task_requests": 0,
                    "mock_request_attempts": 0,
                    "cache_hits": 6,
                    "checkpoint_records_before": 6,
                    "checkpoint_records_after": 6,
                    "checkpoint_unchanged": True,
                    "no_duplicate_success_records": True,
                },
            )
            self.assertEqual(
                {check["role"] for check in evidence["position_checks"]},
                {
                    "first_row",
                    "duplicate_position",
                    "temporary_failure_position",
                    "interruption_boundary",
                    "last_row",
                },
            )
            self.assertTrue(
                all(check["matched"] for check in evidence["position_checks"])
            )
            self.assertEqual(list(temporary_parent.iterdir()), [])

            serialized = json.dumps(
                evidence,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
            self.assertEqual(evidence_path.read_text(encoding="utf-8"), serialized)
            lowered = serialized.lower()
            self.assertNotIn(str(root).lower(), lowered)
            for forbidden in ("http://", "https://", "proxy", "token"):
                self.assertNotIn(forbidden, lowered)

    def test_cli_rejects_less_than_one_thousand_rows(self) -> None:
        """命令行入口不能把小规模开发场景冒充千行验收。"""

        from scripts.validate_synthetic_batch import main

        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            main(("--rows", "999"))

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("至少为 1000", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
