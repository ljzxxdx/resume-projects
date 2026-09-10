"""使用确定性 mock 对生产批处理路径执行离线合成验收。"""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from translation_platform.batch import BatchProcessor, BatchRunner
from translation_platform.batch_input import CellPosition, load_batch_input
from translation_platform.cache import BatchTask, deduplicate_cells
from translation_platform.checkpoint import CheckpointStatus, JsonlCheckpointStore
from translation_platform.errors import NetworkConnectionError
from translation_platform.models import FailureRecord, RecordStatus, TranslationResult
from translation_platform.observability import (
    BatchRunSummary,
    build_run_summary,
    write_run_summary,
)
from translation_platform.retry import RetryExecutor, RetryOutcome, RetryPolicy
from translation_platform.storage import AtomicBatchExporter


_MINIMUM_HELPER_ROWS = 8
_MINIMUM_CLI_ROWS = 1_000
_SOURCE_LANG = "zh-CHS"
_TARGET_LANG = "en"


class _SyntheticInterruption(RuntimeError):
    """仅用于验证断点恢复的一次性离线中断。"""


class _DeterministicMockTranslator:
    """有限且不访问网络的 mock 翻译器，区分任务执行与请求尝试。"""

    def __init__(
        self,
        interruption_key: str,
        temporary_failure_key: str,
    ) -> None:
        self._interruption_key = interruption_key
        self._temporary_failure_key = temporary_failure_key
        self._interruption_injected = False
        self._temporary_failure_injected = False
        self.max_retries = 1
        self.scheduled_retry_delays = []
        self._retry_executor = RetryExecutor(
            policy=RetryPolicy(
                max_retries=self.max_retries,
                base_delay_seconds=0.001,
                max_delay_seconds=0.001,
                jitter_ratio=0.0,
            ),
            sleep=self.scheduled_retry_delays.append,
            random_source=lambda: 0.5,
        )
        self.batch_task_executions = 0
        self.retry_executor_invocations = 0
        self.request_attempts = 0
        self.interruption_attempts = 0
        self.successful_keys = set()
        self.temporary_retry_outcome: Optional[RetryOutcome[str]] = None

    def __call__(self, task: BatchTask):
        self.batch_task_executions += 1
        if (
            task.cache_key == self._interruption_key
            and not self._interruption_injected
        ):
            self._interruption_injected = True
            self.interruption_attempts += 1
            raise _SyntheticInterruption("已注入一次离线合成中断")

        self.retry_executor_invocations += 1
        outcome = self._retry_executor.execute(lambda: self._request(task))
        if task.cache_key == self._temporary_failure_key:
            self.temporary_retry_outcome = outcome

        if outcome.succeeded:
            translated_text = outcome.value
            if not isinstance(translated_text, str):
                raise RuntimeError("生产重试器返回了无效的离线 mock 结果")
            self.successful_keys.add(task.cache_key)
            return TranslationResult(
                run_id="offline-synthetic-mock",
                input_summary="sha256:" + task.cache_key,
                from_lang=task.source_lang,
                to_lang=task.target_lang,
                status=RecordStatus.SUCCESS,
                attempts=outcome.attempts,
                proxy_id_summary=None,
                latency_ms=float(outcome.attempts),
                error_type=None,
                translated_text=translated_text,
            )

        failure = outcome.failure
        if failure is None:
            raise RuntimeError("生产重试器返回了不闭合的离线 mock 失败")
        return FailureRecord(
            run_id="offline-synthetic-mock",
            input_summary="sha256:" + task.cache_key,
            from_lang=task.source_lang,
            to_lang=task.target_lang,
            status=RecordStatus.FAILURE,
            attempts=outcome.attempts,
            proxy_id_summary=None,
            latency_ms=float(outcome.attempts),
            error_type=failure.error_type,
            error_message="离线 mock 在有限生产重试后仍失败",
        )

    def _request(self, task: BatchTask) -> str:
        """模拟一次本地请求；临时故障键只失败一次。"""

        self.request_attempts += 1
        if (
            task.cache_key == self._temporary_failure_key
            and not self._temporary_failure_injected
        ):
            self._temporary_failure_injected = True
            raise NetworkConnectionError("已注入一次离线网络连接失败")
        return "离线译文:" + task.normalized_text[::-1]


def run_synthetic_validation(
    row_count: int,
    evidence_path: Path,
    temporary_parent: Optional[Path] = None,
) -> Dict[str, object]:
    """运行可复现的离线验收，仅把聚合证据写入指定位置。"""

    _validate_row_count(row_count, minimum=_MINIMUM_HELPER_ROWS)
    unique_text_count = max(6, row_count // 4)
    temporary_root = None if temporary_parent is None else Path(temporary_parent)
    if temporary_root is not None:
        temporary_root.mkdir(parents=True, exist_ok=True)

    work_path: Optional[Path] = None
    with tempfile.TemporaryDirectory(
        prefix="translation-platform-synthetic-",
        dir=None if temporary_root is None else str(temporary_root),
    ) as directory:
        work_path = Path(directory)
        source_path = work_path / "synthetic_input.csv"
        output_path = work_path / "synthetic_output.csv"
        second_output_path = work_path / "synthetic_output_second.csv"
        checkpoint_path = work_path / "synthetic_checkpoint.jsonl"
        resume_summary_path = work_path / "resume_summary.json"
        second_summary_path = work_path / "second_summary.json"

        _write_synthetic_input(source_path, row_count, unique_text_count)
        batch_input = load_batch_input(source_path, ("标题", "说明"))
        tasks = deduplicate_cells(
            batch_input.selected_cells,
            _SOURCE_LANG,
            _TARGET_LANG,
        )
        _require(
            len(batch_input.selected_cells) == row_count * 2,
            "合成输入单元格数量不符合公式",
        )
        _require(
            len(tasks) == unique_text_count,
            "合成输入唯一文本数量不符合公式",
        )

        interruption_index = unique_text_count // 2
        temporary_failure_index = interruption_index + 1
        interruption_task = tasks[interruption_index]
        temporary_failure_task = tasks[temporary_failure_index]
        mock_translator = _DeterministicMockTranslator(
            interruption_key=interruption_task.cache_key,
            temporary_failure_key=temporary_failure_task.cache_key,
        )
        store = JsonlCheckpointStore(checkpoint_path)
        runner = BatchRunner(BatchProcessor(mock_translator, max_attempts=2), store)

        interruption_seen = False
        try:
            runner.run(tasks)
        except _SyntheticInterruption:
            interruption_seen = True
        _require(interruption_seen, "离线中断未按预期触发")

        interrupted_records = store.load()
        completed_before_interruption = sum(
            record.status is CheckpointStatus.SUCCESS
            for record in interrupted_records.values()
        )
        pending_after_interruption = sum(
            record.status is CheckpointStatus.PENDING
            for record in interrupted_records.values()
        )
        _require(
            completed_before_interruption == interruption_index,
            "中断前完成任务数量不符合预期",
        )
        _require(
            pending_after_interruption == unique_text_count - interruption_index,
            "中断后的待恢复任务数量不符合预期",
        )

        attempts_before_resume = mock_translator.request_attempts
        executions_before_resume = mock_translator.batch_task_executions
        resume_result = runner.run(tasks)
        resume_mock_attempts = mock_translator.request_attempts - attempts_before_resume
        resume_task_executions = (
            mock_translator.batch_task_executions - executions_before_resume
        )
        _require(
            resume_task_executions == unique_text_count - interruption_index,
            "恢复阶段处理了已完成任务或遗漏了待处理任务",
        )
        _require(
            resume_result.request_count == unique_text_count - interruption_index,
            "恢复阶段批任务请求数不符合预期",
        )
        _require(
            resume_result.cache_hits == interruption_index,
            "恢复阶段缓存命中数不符合预期",
        )
        temporary_retry_outcome = mock_translator.temporary_retry_outcome
        _require(
            temporary_retry_outcome is not None
            and temporary_retry_outcome.succeeded
            and temporary_retry_outcome.attempts == 2,
            "生产重试器未在一次临时失败后恢复",
        )
        _require(
            temporary_retry_outcome.retry_delays == (0.001,)
            and mock_translator.scheduled_retry_delays == [0.001],
            "生产重试器的有限确定性策略不符合预期",
        )

        resume_summary = build_run_summary(
            run_id="offline-synthetic-resume",
            result=resume_result,
            started_at=datetime(2026, 8, 23, 0, 0, 0, tzinfo=timezone.utc),
            finished_at=datetime(2026, 8, 23, 0, 0, 1, tzinfo=timezone.utc),
        )
        write_run_summary(resume_summary_path, resume_summary)
        AtomicBatchExporter().write(
            source_path,
            output_path,
            resume_result.replacements,
        )
        verified_positions = _verify_all_positions(source_path, output_path, row_count)
        position_checks = _build_position_checks(
            source_path=source_path,
            output_path=output_path,
            tasks=tasks,
            temporary_failure_task=temporary_failure_task,
            interruption_task=interruption_task,
            row_count=row_count,
        )

        records_before_second = store.load()
        checkpoint_before_second = checkpoint_path.read_bytes()
        second_mock = _DeterministicMockTranslator(
            interruption_key=interruption_task.cache_key,
            temporary_failure_key=temporary_failure_task.cache_key,
        )
        second_runner = BatchRunner(BatchProcessor(second_mock, max_attempts=2), store)
        second_result = second_runner.run(tasks)
        second_summary = build_run_summary(
            run_id="offline-synthetic-second",
            result=second_result,
            started_at=datetime(2026, 8, 23, 0, 0, 2, tzinfo=timezone.utc),
            finished_at=datetime(2026, 8, 23, 0, 0, 3, tzinfo=timezone.utc),
        )
        write_run_summary(second_summary_path, second_summary)
        AtomicBatchExporter().write(
            source_path,
            second_output_path,
            second_result.replacements,
        )
        _verify_all_positions(source_path, second_output_path, row_count)

        records_after_second = store.load()
        checkpoint_unchanged = checkpoint_path.read_bytes() == checkpoint_before_second
        success_count_before = sum(
            record.status is CheckpointStatus.SUCCESS
            for record in records_before_second.values()
        )
        success_count_after = sum(
            record.status is CheckpointStatus.SUCCESS
            for record in records_after_second.values()
        )
        no_duplicate_success_records = (
            len(records_before_second)
            == len(records_after_second)
            == success_count_before
            == success_count_after
            == unique_text_count
        )
        _require(second_result.request_count == 0, "第二次运行产生了批任务请求")
        _require(second_mock.request_attempts == 0, "第二次运行产生了 mock 请求")
        _require(checkpoint_unchanged, "第二次运行不应改写检查点")
        _require(no_duplicate_success_records, "第二次运行新增了重复成功记录")

        evidence: Dict[str, object] = {
            "schema_version": 1,
            "evidence_type": "offline_synthetic_mock",
            "disclaimer": {
                "offline": True,
                "network_accessed": False,
                "actual_api_calls": 0,
                "row_count_is_actual_api_call_count": False,
            },
            "dataset": {
                "rows": row_count,
                "input_cells": row_count * 2,
                "unique_texts": unique_text_count,
                "deduplicated_cells": row_count * 2 - unique_text_count,
            },
            "fault_injection": {
                "temporary_failure": {
                    "injected": True,
                    "retry_engine": "production_retry_executor",
                    "max_retries": mock_translator.max_retries,
                    "maximum_attempts": mock_translator.max_retries + 1,
                    "attempts_observed": temporary_retry_outcome.attempts,
                    "retries_observed": temporary_retry_outcome.attempts - 1,
                    "retry_delays_seconds": list(
                        temporary_retry_outcome.retry_delays
                    ),
                    "sleep_strategy": "record_only_no_wait",
                    "terminated": temporary_failure_task.cache_key
                    in mock_translator.successful_keys,
                },
                "interruption": {
                    "injected": interruption_seen,
                    "times": mock_translator.interruption_attempts,
                    "completed_before_interruption": completed_before_interruption,
                    "pending_after_interruption": pending_after_interruption,
                },
            },
            "recovery": {
                "resume_task_requests": resume_result.request_count,
                "resume_cache_hits": resume_result.cache_hits,
                "resume_mock_request_attempts": resume_mock_attempts,
                "only_unfinished_tasks_processed": resume_task_executions
                == pending_after_interruption,
                "verified_positions": verified_positions,
                "all_positions_verified": verified_positions == row_count * 2,
            },
            "mock_usage": {
                "unique_task_keys": len(tasks),
                "batch_task_executions": mock_translator.batch_task_executions,
                "retry_executor_invocations": (
                    mock_translator.retry_executor_invocations
                ),
                "request_attempts": mock_translator.request_attempts,
                "successful_unique_tasks": len(mock_translator.successful_keys),
                "temporary_retry_attempts": temporary_retry_outcome.attempts - 1,
                "interruption_attempts": mock_translator.interruption_attempts,
                "actual_api_calls": 0,
            },
            "resume_summary": _summary_payload(resume_summary),
            "second_run_summary": _summary_payload(second_summary),
            "second_identical_run": {
                "batch_task_requests": second_result.request_count,
                "mock_request_attempts": second_mock.request_attempts,
                "cache_hits": second_result.cache_hits,
                "checkpoint_records_before": len(records_before_second),
                "checkpoint_records_after": len(records_after_second),
                "checkpoint_unchanged": checkpoint_unchanged,
                "no_duplicate_success_records": no_duplicate_success_records,
            },
            "position_checks": position_checks,
            "temporary_artifacts": {
                "input_retained": False,
                "complete_output_retained": False,
                "checkpoint_retained": False,
                "run_summaries_retained": False,
            },
        }

    _require(work_path is not None and not work_path.exists(), "临时验收目录清理失败")
    _write_evidence(Path(evidence_path), evidence)
    return evidence


def _validate_row_count(row_count: object, minimum: int) -> None:
    if isinstance(row_count, bool) or not isinstance(row_count, int):
        raise ValueError("合成行数必须是整数")
    if row_count < minimum:
        raise ValueError(f"合成行数必须至少为 {minimum}")


def _write_synthetic_input(path: Path, row_count: int, unique_count: int) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("编号", "标题", "说明"))
        offset = unique_count // 2
        for row_number in range(row_count):
            writer.writerow(
                (
                    f"合成行-{row_number + 1:06d}",
                    f"合成文本-{row_number % unique_count:04d}",
                    f"合成文本-{(row_number + offset) % unique_count:04d}",
                )
            )


def _read_csv(path: Path) -> Tuple[Tuple[str, ...], ...]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return tuple(tuple(row) for row in csv.reader(stream))


def _verify_all_positions(source_path: Path, output_path: Path, row_count: int) -> int:
    source_rows = _read_csv(source_path)
    output_rows = _read_csv(output_path)
    _require(len(source_rows) == len(output_rows) == row_count + 1, "导出行数不正确")
    verified = 0
    for row_index in range(1, row_count + 1):
        _require(source_rows[row_index][0] == output_rows[row_index][0], "未选中列被改写")
        for column_index in (1, 2):
            # 期望值直接由原输入反转生成，不复用 mock 或被测统计辅助函数。
            expected = "离线译文:" + source_rows[row_index][column_index][::-1]
            _require(
                output_rows[row_index][column_index] == expected,
                f"第 {row_index + 1} 行第 {column_index + 1} 列回填错误",
            )
            verified += 1
    return verified


def _build_position_checks(
    source_path: Path,
    output_path: Path,
    tasks: Sequence[BatchTask],
    temporary_failure_task: BatchTask,
    interruption_task: BatchTask,
    row_count: int,
) -> Tuple[Dict[str, object], ...]:
    first_task = tasks[0]
    _require(len(first_task.positions) >= 2, "首个合成任务没有重复位置")
    positions = (
        ("first_row", CellPosition(2, 2, "标题")),
        ("duplicate_position", first_task.positions[1]),
        ("temporary_failure_position", temporary_failure_task.positions[0]),
        ("interruption_boundary", interruption_task.positions[0]),
        ("last_row", CellPosition(row_count + 1, 3, "说明")),
    )
    source_rows = _read_csv(source_path)
    output_rows = _read_csv(output_path)
    checks = []
    for role, position in positions:
        source_value = source_rows[position.row_index - 1][position.column_index - 1]
        output_value = output_rows[position.row_index - 1][position.column_index - 1]
        checks.append(
            {
                "role": role,
                "row_index": position.row_index,
                "column_index": position.column_index,
                "matched": output_value == "离线译文:" + source_value[::-1],
            }
        )
    return tuple(checks)


def _summary_payload(summary: BatchRunSummary) -> Dict[str, object]:
    return {
        "run_id": summary.run_id,
        "input_cells": summary.input_cells,
        "unique_texts": summary.unique_texts,
        "successes": summary.successes,
        "failures": summary.failures,
        "cache_hits": summary.cache_hits,
        "retries": summary.retries,
        "error_counts": dict(summary.error_counts),
        "started_at": summary.started_at,
        "finished_at": summary.finished_at,
        "average_latency_ms": summary.average_latency_ms,
        "p95_latency_ms": summary.p95_latency_ms,
    }


def _write_evidence(path: Path, evidence: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(
                evidence,
                stream,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(path))
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行千行合成批处理离线验收")
    parser.add_argument("--rows", type=int, default=_MINIMUM_CLI_ROWS, help="合成数据行数")
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "artifacts"
        / "validation"
        / "synthetic_batch_evidence.json",
        help="聚合证据输出路径",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口只接受至少一千行的正式离线验收。"""

    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.rows < _MINIMUM_CLI_ROWS:
        parser.error("--rows 必须至少为 1000")
    evidence = run_synthetic_validation(
        row_count=arguments.rows,
        evidence_path=arguments.evidence_path,
    )
    print(
        json.dumps(
            {
                "evidence_type": evidence["evidence_type"],
                "rows": evidence["dataset"]["rows"],
                "input_cells": evidence["dataset"]["input_cells"],
                "unique_texts": evidence["dataset"]["unique_texts"],
                "mock_request_attempts": evidence["mock_usage"]["request_attempts"],
                "second_run_mock_requests": evidence["second_identical_run"][
                    "mock_request_attempts"
                ],
                "network_accessed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
