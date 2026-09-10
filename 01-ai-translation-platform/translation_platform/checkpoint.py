"""用于恢复和幂等执行的 JSONL 检查点持久层。"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Dict, List, Mapping, Optional, Tuple

from translation_platform.errors import ConfigurationFailure, ErrorType


class CheckpointValidationError(ConfigurationFailure, ValueError):
    """检查点记录的字段或状态组合不符合约束时抛出。"""


class CheckpointFormatError(ConfigurationFailure, ValueError):
    """检查点文件不是受支持的严格 JSONL 格式时抛出。"""


class CheckpointStatus(str, Enum):
    """单个缓存键在可恢复执行中的处理状态。"""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class CheckpointRecord:
    """只保留恢复所需、且不含源文本和私密运行参数的状态。"""

    cache_key: str
    status: CheckpointStatus
    attempts: int
    translated_text: Optional[str]
    error_type: Optional[ErrorType]
    latency_ms: float

    def __post_init__(self) -> None:
        cache_key = _normalize_cache_key(self.cache_key)
        object.__setattr__(self, "cache_key", cache_key)

        if not isinstance(self.status, CheckpointStatus):
            raise CheckpointValidationError("status 必须是 CheckpointStatus 枚举值")

        _validate_attempts(self.attempts)
        _validate_latency(self.latency_ms)

        if self.status is CheckpointStatus.PENDING:
            self._validate_pending()
        elif self.status is CheckpointStatus.SUCCESS:
            self._validate_success()
        else:
            self._validate_failure()

    def _validate_pending(self) -> None:
        if self.attempts != 0 or self.latency_ms != 0:
            raise CheckpointValidationError("待处理记录的 attempts 和 latency_ms 必须为 0")
        if self.translated_text is not None or self.error_type is not None:
            raise CheckpointValidationError("待处理记录不能包含结果或错误类型")

    def _validate_success(self) -> None:
        if self.attempts < 1:
            raise CheckpointValidationError("成功记录的 attempts 必须至少为 1")
        if self.error_type is not None:
            raise CheckpointValidationError("成功记录不能包含错误类型")
        translated_text = _normalize_translated_text(self.translated_text)
        object.__setattr__(self, "translated_text", translated_text)

    def _validate_failure(self) -> None:
        if self.attempts < 1:
            raise CheckpointValidationError("失败记录的 attempts 必须至少为 1")
        if self.translated_text is not None:
            raise CheckpointValidationError("失败记录不能包含翻译结果")
        if not isinstance(self.error_type, ErrorType):
            raise CheckpointValidationError("失败记录必须包含 ErrorType 错误类型")


class JsonlCheckpointStore:
    """以同目录临时文件和原子替换保存检查点的 JSONL 存储。"""

    _FIELD_NAMES = frozenset(
        (
            "cache_key",
            "status",
            "attempts",
            "translated_text",
            "error_type",
            "latency_ms",
        )
    )

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Dict[str, CheckpointRecord]:
        """加载记录；重复缓存键以文件中最后一条记录为准。"""

        if not self.path.exists():
            return {}

        records: Dict[str, CheckpointRecord] = {}
        try:
            with self.path.open("r", encoding="utf-8") as checkpoint_file:
                for line_number, line in enumerate(checkpoint_file, start=1):
                    record = self._parse_line(line, line_number)
                    records[record.cache_key] = record
        except UnicodeDecodeError as exc:
            raise CheckpointFormatError("检查点文件不是 UTF-8 文本") from exc
        return records

    def save(self, records: Mapping[str, CheckpointRecord]) -> None:
        """完整写入记录并在成功落盘后原子替换旧检查点。"""

        normalized_records = self._validate_records(records)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.path.parent),
                prefix="." + self.path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                for record in normalized_records.values():
                    temporary_file.write(self._serialize_record(record))
                    temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(str(temporary_path), str(self.path))
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def _parse_line(self, line: str, line_number: int) -> CheckpointRecord:
        try:
            parsed = json.loads(
                line,
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_non_finite_json_number,
            )
            if not isinstance(parsed, dict):
                raise CheckpointValidationError("每行必须是 JSON 对象")
            if set(parsed) != self._FIELD_NAMES:
                raise CheckpointValidationError("JSON 字段必须与检查点格式完全一致")
            return CheckpointRecord(
                cache_key=parsed["cache_key"],
                status=CheckpointStatus(parsed["status"]),
                attempts=parsed["attempts"],
                translated_text=parsed["translated_text"],
                error_type=(
                    None
                    if parsed["error_type"] is None
                    else ErrorType(parsed["error_type"])
                ),
                latency_ms=parsed["latency_ms"],
            )
        except (CheckpointValidationError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CheckpointFormatError(f"检查点第 {line_number} 行格式错误: {exc}") from exc

    def _validate_records(
        self,
        records: Mapping[str, CheckpointRecord],
    ) -> Dict[str, CheckpointRecord]:
        if not isinstance(records, Mapping):
            raise CheckpointValidationError("records 必须是以缓存键为键的映射")

        normalized_records: Dict[str, CheckpointRecord] = {}
        for cache_key, record in records.items():
            normalized_key = _normalize_cache_key(cache_key)
            if not isinstance(record, CheckpointRecord):
                raise CheckpointValidationError("records 的值必须是 CheckpointRecord")
            if normalized_key != record.cache_key:
                raise CheckpointValidationError("映射键必须与记录的 cache_key 一致")
            normalized_records[normalized_key] = record
        return normalized_records

    @staticmethod
    def _serialize_record(record: CheckpointRecord) -> str:
        return json.dumps(
            {
                "cache_key": record.cache_key,
                "status": record.status.value,
                "attempts": record.attempts,
                "translated_text": record.translated_text,
                "error_type": (
                    None if record.error_type is None else record.error_type.value
                ),
                "latency_ms": record.latency_ms,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )


def _normalize_cache_key(value: object) -> str:
    if not isinstance(value, str):
        raise CheckpointValidationError("cache_key 必须是文本")
    normalized_value = value.strip()
    if not normalized_value:
        raise CheckpointValidationError("cache_key 不能为空")
    if _is_absolute_path(normalized_value):
        raise CheckpointValidationError("cache_key 不能是绝对输入路径")
    return normalized_value


def _normalize_translated_text(value: object) -> str:
    if not isinstance(value, str):
        raise CheckpointValidationError("成功记录的 translated_text 必须是文本")
    normalized_value = value.strip()
    if not normalized_value:
        raise CheckpointValidationError("成功记录的 translated_text 不能为空")
    return normalized_value


def _validate_attempts(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CheckpointValidationError("attempts 必须是非负整数")


def _validate_latency(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CheckpointValidationError("latency_ms 必须是数值")
    if not math.isfinite(value) or value < 0:
        raise CheckpointValidationError("latency_ms 必须是非负有限数值")


def _is_absolute_path(value: str) -> bool:
    return (
        os.path.isabs(value)
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
    )


def _reject_non_finite_json_number(value: str) -> None:
    raise ValueError(f"不允许非有限 JSON 数值: {value}")


def _strict_json_object(pairs: List[Tuple[str, object]]) -> Dict[str, object]:
    """拒绝重复字段，避免解析器静默覆盖检查点内容。"""

    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointValidationError(f"JSON 对象包含重复字段: {key}")
        result[key] = value
    return result
