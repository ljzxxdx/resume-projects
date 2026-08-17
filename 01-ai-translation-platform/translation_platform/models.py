"""AI 翻译平台的不可变运行数据模型。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from translation_platform.config import ConfigurationError, validate_language_pair


class ModelValidationError(ValueError):
    """运行记录不满足一致性约束时抛出。"""


class RecordStatus(str, Enum):
    """单条记录或整次运行的结果状态。"""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"


class ErrorType(str, Enum):
    """供请求、批处理和运行摘要共用的错误分类。"""

    CONFIGURATION = "configuration"
    SIGNATURE_TOKEN = "signature_token"
    NETWORK_CONNECTION = "network_connection"
    TIMEOUT = "timeout"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    RATE_LIMIT = "rate_limit"
    RESPONSE_FORMAT = "response_format"
    EMPTY_RESULT = "empty_result"


@dataclass(frozen=True)
class TranslationResult:
    """一次成功翻译及其脱敏追踪信息。"""

    run_id: str
    input_summary: str
    from_lang: str
    to_lang: str
    status: RecordStatus
    attempts: int
    proxy_id_summary: Optional[str]
    latency_ms: float
    error_type: Optional[ErrorType]
    translated_text: str

    def __post_init__(self) -> None:
        _normalize_and_validate_trace(self, minimum_attempts=1)
        if self.status is not RecordStatus.SUCCESS:
            raise ModelValidationError("translation result status must be success")
        if self.error_type is not None:
            raise ModelValidationError("successful translation must not have an error type")
        normalized_text = _normalize_required_text(
            "translated_text",
            self.translated_text,
        )
        object.__setattr__(self, "translated_text", normalized_text)


@dataclass(frozen=True)
class FailureRecord:
    """一次失败翻译及其错误分类和脱敏追踪信息。"""

    run_id: str
    input_summary: str
    from_lang: str
    to_lang: str
    status: RecordStatus
    attempts: int
    proxy_id_summary: Optional[str]
    latency_ms: float
    error_type: Optional[ErrorType]
    error_message: str

    def __post_init__(self) -> None:
        _normalize_and_validate_trace(self, minimum_attempts=1)
        if self.status is not RecordStatus.FAILURE:
            raise ModelValidationError("failure record status must be failure")
        if not isinstance(self.error_type, ErrorType):
            raise ModelValidationError("failure record requires an error type")
        normalized_message = _normalize_required_text(
            "error_message",
            self.error_message,
        )
        object.__setattr__(self, "error_message", normalized_message)


@dataclass(frozen=True)
class RunSummary:
    """一次运行的结果状态和聚合追踪信息。"""

    run_id: str
    input_summary: str
    from_lang: str
    to_lang: str
    status: RecordStatus
    attempts: int
    proxy_id_summary: Optional[str]
    latency_ms: float
    error_type: Optional[ErrorType]

    def __post_init__(self) -> None:
        _normalize_and_validate_trace(self, minimum_attempts=0)
        if self.status is RecordStatus.SUCCESS and self.error_type is not None:
            raise ModelValidationError("successful run must not have an error type")
        if self.status is RecordStatus.FAILURE and not isinstance(
            self.error_type,
            ErrorType,
        ):
            raise ModelValidationError("failed run requires an error type")


def _normalize_and_validate_trace(record: object, minimum_attempts: int) -> None:
    run_id = _normalize_required_text("run_id", getattr(record, "run_id"))
    input_summary = _normalize_required_text(
        "input_summary",
        getattr(record, "input_summary"),
    )
    object.__setattr__(record, "run_id", run_id)
    object.__setattr__(record, "input_summary", input_summary)

    try:
        validate_language_pair(
            getattr(record, "from_lang"),
            getattr(record, "to_lang"),
        )
    except ConfigurationError as exc:
        raise ModelValidationError(str(exc)) from exc

    status = getattr(record, "status")
    if not isinstance(status, RecordStatus):
        raise ModelValidationError("status must be a RecordStatus value")

    attempts = getattr(record, "attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int):
        raise ModelValidationError("attempts must be an integer")
    if attempts < minimum_attempts:
        raise ModelValidationError(f"attempts must be at least {minimum_attempts}")

    latency_ms = getattr(record, "latency_ms")
    if isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float)):
        raise ModelValidationError("latency_ms must be a number")
    if not math.isfinite(latency_ms) or latency_ms < 0:
        raise ModelValidationError("latency_ms must be non-negative and finite")

    error_type = getattr(record, "error_type")
    if error_type is not None and not isinstance(error_type, ErrorType):
        raise ModelValidationError("error_type must be an ErrorType value or None")

    proxy_summary = getattr(record, "proxy_id_summary")
    if proxy_summary is not None:
        if not isinstance(proxy_summary, str):
            raise ModelValidationError("proxy_id_summary must be text or None")
        normalized_proxy_summary = proxy_summary.strip()
        if not normalized_proxy_summary:
            raise ModelValidationError("proxy_id_summary must not be empty")
        object.__setattr__(record, "proxy_id_summary", normalized_proxy_summary)


def _normalize_required_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise ModelValidationError(f"{name} must be text")
    normalized = value.strip()
    if not normalized:
        raise ModelValidationError(f"{name} must not be empty")
    return normalized
