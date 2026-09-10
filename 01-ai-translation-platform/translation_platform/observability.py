"""生成不含业务原文和私密运行参数的批量运行摘要。"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Tuple

from translation_platform.batch import BatchRunResult
from translation_platform.checkpoint import CheckpointStatus
from translation_platform.errors import ConfigurationFailure, ErrorType


_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_ERROR_TYPE_VALUES = frozenset(error_type.value for error_type in ErrorType)


@dataclass(frozen=True)
class BatchRunSummary:
    """一次批量运行的固定字段、深度只读汇总。"""

    run_id: str
    input_cells: int
    unique_texts: int
    successes: int
    failures: int
    cache_hits: int
    retries: int
    error_counts: Mapping[str, int]
    started_at: str
    finished_at: str
    average_latency_ms: float
    p95_latency_ms: float

    def __post_init__(self) -> None:
        run_id = _normalize_run_id(self.run_id)

        for field_name in (
            "input_cells",
            "unique_texts",
            "successes",
            "failures",
            "cache_hits",
            "retries",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigurationFailure(field_name + " 必须是非负整数")

        if self.successes + self.failures != self.unique_texts:
            raise ConfigurationFailure("successes + failures 必须等于 unique_texts")
        if self.cache_hits > self.unique_texts:
            raise ConfigurationFailure("cache_hits 不能超过 unique_texts")

        if not isinstance(self.error_counts, Mapping):
            raise ConfigurationFailure("error_counts 必须是错误分类映射")
        normalized_error_counts: Dict[str, int] = {}
        for error_type, count in self.error_counts.items():
            if error_type not in _ERROR_TYPE_VALUES:
                raise ConfigurationFailure("error_counts 的键必须是 ErrorType 值")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ConfigurationFailure("error_counts 的值必须是正整数")
            normalized_error_counts[error_type] = count
        if sum(normalized_error_counts.values()) != self.failures:
            raise ConfigurationFailure("error_counts 总和必须等于 failures")

        started_at = _parse_timestamp("started_at", self.started_at)
        finished_at = _parse_timestamp("finished_at", self.finished_at)
        if started_at > finished_at:
            raise ConfigurationFailure("started_at 不能晚于 finished_at")

        for field_name in ("average_latency_ms", "p95_latency_ms"):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ConfigurationFailure(field_name + " 必须是非负有限数值")

        object.__setattr__(self, "run_id", run_id)
        object.__setattr__(
            self,
            "error_counts",
            MappingProxyType(dict(normalized_error_counts)),
        )


def build_run_summary(
    run_id: str,
    result: BatchRunResult,
    started_at: datetime,
    finished_at: datetime,
) -> BatchRunSummary:
    """按最终唯一任务统计结果，并只用本轮执行任务统计耗时。"""

    if not isinstance(result, BatchRunResult):
        raise ConfigurationFailure("result 必须是 BatchRunResult")
    _validate_aware_datetime("started_at", started_at)
    _validate_aware_datetime("finished_at", finished_at)
    if started_at > finished_at:
        raise ConfigurationFailure("started_at 不能晚于 finished_at")

    outcomes = tuple(result.outcomes)
    outcome_by_key = {}
    successes = 0
    failures = 0
    error_counts: Dict[str, int] = {}
    input_cells = 0
    for outcome in outcomes:
        task_key = outcome.task.cache_key
        if task_key != outcome.record.cache_key:
            raise ConfigurationFailure("outcome 的任务键与检查点键不一致")
        if task_key in outcome_by_key:
            raise ConfigurationFailure("outcomes 不能包含重复 cache_key")
        outcome_by_key[task_key] = outcome
        input_cells += len(outcome.task.positions)

        if outcome.record.status is CheckpointStatus.SUCCESS:
            successes += 1
        elif outcome.record.status is CheckpointStatus.FAILURE:
            failures += 1
            error_type = outcome.record.error_type
            if error_type is None:
                raise ConfigurationFailure("失败 outcome 必须包含错误类型")
            error_name = error_type.value
            error_counts[error_name] = error_counts.get(error_name, 0) + 1
        else:
            raise ConfigurationFailure("运行摘要不能包含 pending outcome")

    unique_texts = len(outcomes)
    cache_hits = _validate_counter("cache_hits", result.cache_hits)
    request_count = _validate_counter("request_count", result.request_count)
    if request_count + cache_hits != unique_texts:
        raise ConfigurationFailure(
            "request_count + cache_hits 必须等于 unique_texts"
        )

    executed_cache_keys = tuple(result.executed_cache_keys)
    if len(executed_cache_keys) != request_count:
        raise ConfigurationFailure(
            "executed_cache_keys 数量必须等于 request_count"
        )
    if len(set(executed_cache_keys)) != len(executed_cache_keys):
        raise ConfigurationFailure("executed_cache_keys 不能包含重复项")
    if any(cache_key not in outcome_by_key for cache_key in executed_cache_keys):
        raise ConfigurationFailure("executed_cache_keys 只能引用本轮 outcomes")

    executed_records = tuple(
        outcome_by_key[cache_key].record for cache_key in executed_cache_keys
    )
    retries = sum(record.attempts - 1 for record in executed_records)
    latencies = tuple(float(record.latency_ms) for record in executed_records)
    average_latency_ms, p95_latency_ms = _latency_statistics(latencies)

    return BatchRunSummary(
        run_id=run_id,
        input_cells=input_cells,
        unique_texts=unique_texts,
        successes=successes,
        failures=failures,
        cache_hits=cache_hits,
        retries=retries,
        error_counts=error_counts,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        average_latency_ms=average_latency_ms,
        p95_latency_ms=p95_latency_ms,
    )


def write_run_summary(path: Path, summary: BatchRunSummary) -> None:
    """通过同目录临时文件原子替换摘要，失败时保留原文件。"""

    if not isinstance(summary, BatchRunSummary):
        raise ConfigurationFailure("summary 必须是 BatchRunSummary")
    validated_summary = BatchRunSummary(**_summary_payload(summary))
    output_path = Path(path)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(output_path.parent),
            prefix="." + output_path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(
                _summary_payload(validated_summary),
                temporary_file,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(str(temporary_path), str(output_path))
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


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


def _validate_counter(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigurationFailure(name + " 必须是非负整数")
    return value


def _normalize_run_id(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigurationFailure("run_id 必须是文本")
    normalized_value = value.strip()
    if _RUN_ID_PATTERN.fullmatch(normalized_value) is None:
        raise ConfigurationFailure(
            "run_id 必须以 ASCII 字母或数字开头，且只能包含字母、数字、点、下划线和连字符，长度不能超过 128"
        )
    return normalized_value


def _validate_aware_datetime(name: str, value: object) -> None:
    if not isinstance(value, datetime):
        raise ConfigurationFailure(name + " 必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfigurationFailure(name + " 必须包含时区")


def _parse_timestamp(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise ConfigurationFailure(name + " 必须是 ISO 8601 文本")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ConfigurationFailure(name + " 必须是有效 ISO 8601 文本") from exc
    _validate_aware_datetime(name, timestamp)
    return timestamp


def _latency_statistics(latencies: Tuple[float, ...]) -> Tuple[float, float]:
    if not latencies:
        return 0.0, 0.0
    ordered_latencies = sorted(latencies)
    p95_index = math.ceil(0.95 * len(ordered_latencies)) - 1
    return (
        sum(ordered_latencies) / len(ordered_latencies),
        ordered_latencies[p95_index],
    )
