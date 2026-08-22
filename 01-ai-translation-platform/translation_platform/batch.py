"""将缓存任务的翻译结果转换为可恢复的最终检查点状态。"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple, Union

from translation_platform.batch_input import CellPosition
from translation_platform.cache import BatchTask
from translation_platform.checkpoint import (
    CheckpointRecord,
    CheckpointStatus,
    JsonlCheckpointStore,
)
from translation_platform.errors import ConfigurationFailure, EmptyTranslationError, ErrorType
from translation_platform.models import FailureRecord, TranslationResult


TranslationOutcome = Union[TranslationResult, FailureRecord]
Translator = Callable[[BatchTask], TranslationOutcome]


@dataclass(frozen=True)
class BatchTaskOutcome:
    """一项缓存任务及其唯一最终检查点记录。

    ``error_message`` 仅用于适配层产生的诊断，且始终不含源文本。
    """

    task: BatchTask
    record: CheckpointRecord
    error_message: Optional[str] = None


@dataclass(frozen=True)
class BatchRunResult:
    """一次可恢复批量运行的只读结果。"""

    outcomes: Tuple[BatchTaskOutcome, ...]
    replacements: Mapping[CellPosition, str]
    cache_hits: int
    request_count: int
    executed_cache_keys: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("request_count", "cache_hits"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigurationFailure(field_name + " 必须是非负整数")

        outcomes = tuple(self.outcomes)
        executed_cache_keys = tuple(self.executed_cache_keys)
        if len(executed_cache_keys) != self.request_count:
            raise ConfigurationFailure(
                "executed_cache_keys 数量必须等于 request_count"
            )
        if len(set(executed_cache_keys)) != len(executed_cache_keys):
            raise ConfigurationFailure("executed_cache_keys 不能包含重复项")
        outcome_cache_keys = {outcome.task.cache_key for outcome in outcomes}
        if any(cache_key not in outcome_cache_keys for cache_key in executed_cache_keys):
            raise ConfigurationFailure("executed_cache_keys 只能引用本轮 outcomes")

        object.__setattr__(self, "outcomes", outcomes)
        object.__setattr__(
            self,
            "executed_cache_keys",
            executed_cache_keys,
        )
        object.__setattr__(
            self,
            "replacements",
            MappingProxyType(dict(self.replacements)),
        )


class BatchProcessor:
    """消费已完成重试的翻译函数，不在此处发起新的重试。"""

    def __init__(self, translator: Translator, max_attempts: int) -> None:
        if not callable(translator):
            raise ConfigurationFailure("translator 必须是可调用对象")
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 6
        ):
            raise ConfigurationFailure("max_attempts 必须在 1 到 6 之间")
        self._translator = translator
        self._max_attempts = max_attempts

    def process(self, task: BatchTask) -> BatchTaskOutcome:
        """处理一次任务，并将任何可预期异常归入唯一最终状态。"""

        try:
            result = self._translator(task)
        except EmptyTranslationError:
            return self._failure(
                task,
                attempts=1,
                latency_ms=0.0,
                error_type=ErrorType.EMPTY_RESULT,
                error_message="翻译响应为空",
            )

        if not isinstance(result, (TranslationResult, FailureRecord)):
            return self._failure(
                task,
                attempts=1,
                latency_ms=0.0,
                error_type=ErrorType.RESPONSE_FORMAT,
                error_message="翻译器返回了不支持的结果类型",
            )

        if result.attempts > self._max_attempts:
            return self._failure(
                task,
                attempts=self._max_attempts,
                latency_ms=result.latency_ms,
                error_type=ErrorType.RESPONSE_FORMAT,
                error_message="翻译器回传次数超过上限",
            )

        if not self._matches_task(task, result):
            return self._failure(
                task,
                attempts=result.attempts,
                latency_ms=result.latency_ms,
                error_type=ErrorType.RESPONSE_FORMAT,
                error_message="翻译器回传的任务追踪信息不匹配",
            )

        if isinstance(result, FailureRecord):
            return BatchTaskOutcome(
                task=task,
                record=CheckpointRecord(
                    cache_key=task.cache_key,
                    status=CheckpointStatus.FAILURE,
                    attempts=result.attempts,
                    translated_text=None,
                    error_type=result.error_type,
                    latency_ms=result.latency_ms,
                ),
            )

        if not isinstance(result.translated_text, str) or not result.translated_text.strip():
            return self._failure(
                task,
                attempts=result.attempts,
                latency_ms=result.latency_ms,
                error_type=ErrorType.EMPTY_RESULT,
                error_message="翻译响应为空",
            )

        return BatchTaskOutcome(
            task=task,
            record=CheckpointRecord(
                cache_key=task.cache_key,
                status=CheckpointStatus.SUCCESS,
                attempts=result.attempts,
                translated_text=result.translated_text,
                error_type=None,
                latency_ms=result.latency_ms,
            ),
        )

    @staticmethod
    def _matches_task(task: BatchTask, result: TranslationOutcome) -> bool:
        return (
            result.from_lang == task.source_lang
            and result.to_lang == task.target_lang
            and result.input_summary == "sha256:" + task.cache_key
        )

    @staticmethod
    def _failure(
        task: BatchTask,
        attempts: int,
        latency_ms: float,
        error_type: ErrorType,
        error_message: str,
    ) -> BatchTaskOutcome:
        return BatchTaskOutcome(
            task=task,
            record=CheckpointRecord(
                cache_key=task.cache_key,
                status=CheckpointStatus.FAILURE,
                attempts=attempts,
                translated_text=None,
                error_type=error_type,
                latency_ms=latency_ms,
            ),
            error_message=error_message,
        )


class BatchRunner:
    """根据检查点选择任务，并在每项完成后立即保存恢复状态。"""

    def __init__(
        self,
        processor: BatchProcessor,
        checkpoint_store: JsonlCheckpointStore,
    ) -> None:
        self._processor = processor
        self._checkpoint_store = checkpoint_store

    def run(
        self,
        tasks: Iterable[BatchTask],
        retry_failures: bool = False,
    ) -> BatchRunResult:
        """运行新任务和待处理任务，并按开关决定是否重试既有失败。"""

        if not isinstance(retry_failures, bool):
            raise ConfigurationFailure("retry_failures 必须是布尔值")

        ordered_tasks = tuple(tasks)
        self._reject_duplicate_cache_keys(ordered_tasks)
        records = self._checkpoint_store.load()
        execution_keys = self._select_execution_keys(
            ordered_tasks,
            records,
            retry_failures,
        )

        if execution_keys:
            for task in ordered_tasks:
                if task.cache_key in execution_keys:
                    records[task.cache_key] = self._pending_record(task.cache_key)
            # 在任何翻译请求之前保存全部待执行项，确保中断后仍可恢复。
            self._checkpoint_store.save(records)

        outcomes = []
        replacements: Dict[CellPosition, str] = {}
        cache_hits = 0
        request_count = 0
        for task in ordered_tasks:
            if task.cache_key in execution_keys:
                request_count += 1
                outcome = self._processor.process(task)
                records[task.cache_key] = outcome.record
                self._checkpoint_store.save(records)
            else:
                cache_hits += 1
                outcome = BatchTaskOutcome(task=task, record=records[task.cache_key])

            outcomes.append(outcome)
            if outcome.record.status is CheckpointStatus.SUCCESS:
                translated_text = outcome.record.translated_text
                if translated_text is not None:
                    for position in task.positions:
                        replacements[position] = translated_text

        return BatchRunResult(
            outcomes=tuple(outcomes),
            replacements=replacements,
            cache_hits=cache_hits,
            request_count=request_count,
            executed_cache_keys=tuple(
                task.cache_key
                for task in ordered_tasks
                if task.cache_key in execution_keys
            ),
        )

    @staticmethod
    def _reject_duplicate_cache_keys(tasks: Tuple[BatchTask, ...]) -> None:
        seen = set()
        for task in tasks:
            if task.cache_key in seen:
                raise ConfigurationFailure(
                    "tasks 不能包含重复 cache_key：" + task.cache_key
                )
            seen.add(task.cache_key)

    @staticmethod
    def _select_execution_keys(
        tasks: Tuple[BatchTask, ...],
        records: Mapping[str, CheckpointRecord],
        retry_failures: bool,
    ) -> set:
        execution_keys = set()
        for task in tasks:
            record = records.get(task.cache_key)
            if (
                record is None
                or record.status is CheckpointStatus.PENDING
                or (
                    retry_failures
                    and record.status is CheckpointStatus.FAILURE
                )
            ):
                execution_keys.add(task.cache_key)
        return execution_keys

    @staticmethod
    def _pending_record(cache_key: str) -> CheckpointRecord:
        return CheckpointRecord(
            cache_key=cache_key,
            status=CheckpointStatus.PENDING,
            attempts=0,
            translated_text=None,
            error_type=None,
            latency_ms=0.0,
        )
