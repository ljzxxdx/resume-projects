"""有限指数退避、抖动和结构化失败结果。"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Callable, Generic, Optional, Tuple, TypeVar

from translation_platform.errors import (
    ConfigurationFailure,
    ErrorType,
    Http5xxError,
    NetworkConnectionError,
    RequestTimeoutError,
    TranslationPlatformError,
)


ResultT = TypeVar("ResultT")
RECOVERABLE_5XX = frozenset({500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """一次操作允许的有限重试和等待参数。"""

    max_retries: int
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_retries, bool)
            or not isinstance(self.max_retries, int)
            or not 0 <= self.max_retries <= 5
        ):
            raise ConfigurationFailure("max_retries must be between 0 and 5")
        _validate_positive_finite("base_delay_seconds", self.base_delay_seconds)
        _validate_positive_finite("max_delay_seconds", self.max_delay_seconds)
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ConfigurationFailure(
                "max_delay_seconds must not be less than base_delay_seconds"
            )
        if (
            isinstance(self.jitter_ratio, bool)
            or not isinstance(self.jitter_ratio, (int, float))
            or not math.isfinite(self.jitter_ratio)
            or not 0 <= self.jitter_ratio <= 1
        ):
            raise ConfigurationFailure("jitter_ratio must be between 0 and 1")


@dataclass(frozen=True)
class RetryFailure:
    """不可恢复或重试耗尽后的结构化失败。"""

    error_type: ErrorType
    message: str
    attempts: int
    retry_delays: Tuple[float, ...]
    proxy_switches: int
    exhausted: bool
    last_error: TranslationPlatformError


@dataclass(frozen=True)
class RetryOutcome(Generic[ResultT]):
    """有限重试执行的成功值或失败信息。"""

    value: Optional[ResultT]
    failure: Optional[RetryFailure]
    attempts: int
    retry_delays: Tuple[float, ...]
    proxy_switches: int

    @property
    def succeeded(self) -> bool:
        return self.failure is None


class RetryExecutor:
    """只重试明确可恢复错误，并在有限循环内结束。"""

    def __init__(
        self,
        policy: RetryPolicy,
        sleep: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
    ) -> None:
        if not isinstance(policy, RetryPolicy):
            raise ConfigurationFailure("RetryPolicy is required")
        if not callable(sleep) or not callable(random_source):
            raise ConfigurationFailure("sleep and random_source must be callable")
        self._policy = policy
        self._sleep = sleep
        self._random_source = random_source

    def execute(self, operation: Callable[[], ResultT]) -> RetryOutcome[ResultT]:
        """执行操作；成功立即返回，失败最多等待并重试配置次数。"""

        if not callable(operation):
            raise ConfigurationFailure("retry operation must be callable")
        retry_delays = []
        proxy_switches = 0
        maximum_attempts = self._policy.max_retries + 1

        for attempt in range(1, maximum_attempts + 1):
            try:
                return RetryOutcome(
                    value=operation(),
                    failure=None,
                    attempts=attempt,
                    retry_delays=tuple(retry_delays),
                    proxy_switches=proxy_switches,
                )
            except TranslationPlatformError as exc:
                proxy_switches += _proxy_switch_count(exc)
                retryable = _is_retryable(exc)
                if retryable and attempt < maximum_attempts:
                    delay = self._delay_for_retry(attempt - 1)
                    retry_delays.append(delay)
                    self._sleep(delay)
                    continue
                return RetryOutcome(
                    value=None,
                    failure=RetryFailure(
                        error_type=exc.error_type,
                        message=str(exc),
                        attempts=attempt,
                        retry_delays=tuple(retry_delays),
                        proxy_switches=proxy_switches,
                        exhausted=retryable and attempt == maximum_attempts,
                        last_error=exc,
                    ),
                    attempts=attempt,
                    retry_delays=tuple(retry_delays),
                    proxy_switches=proxy_switches,
                )

        raise AssertionError("finite retry loop ended without an outcome")

    def _delay_for_retry(self, retry_index: int) -> float:
        random_value = self._random_source()
        if (
            isinstance(random_value, bool)
            or not isinstance(random_value, (int, float))
            or not math.isfinite(random_value)
            or not 0 <= random_value <= 1
        ):
            raise ConfigurationFailure("random_source must return a value from 0 to 1")
        exponential = min(
            self._policy.base_delay_seconds * (2 ** retry_index),
            self._policy.max_delay_seconds,
        )
        factor = (
            1
            - self._policy.jitter_ratio
            + 2 * self._policy.jitter_ratio * random_value
        )
        return min(exponential * factor, self._policy.max_delay_seconds)


def _is_retryable(error: TranslationPlatformError) -> bool:
    if isinstance(error, (NetworkConnectionError, RequestTimeoutError)):
        return True
    return (
        isinstance(error, Http5xxError)
        and error.status_code in RECOVERABLE_5XX
    )


def _proxy_switch_count(error: TranslationPlatformError) -> int:
    value = getattr(error, "proxy_switches", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _validate_positive_finite(name: str, value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigurationFailure(f"{name} must be a positive finite number")
