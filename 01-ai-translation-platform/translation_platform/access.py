"""Retry-After 优先和有限代理备用访问策略。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Generic, Optional, TypeVar

from translation_platform.errors import (
    AccessRestrictionError,
    ConfigurationFailure,
    ErrorType,
    RateLimitError,
    TranslationPlatformError,
)
from translation_platform.token import AuthenticationFailure, TokenRefreshExhausted


ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class AccessPolicy:
    """代理备用是否启用及单条任务的最大切换次数。"""

    proxy_fallback_enabled: bool = False
    max_proxy_switches: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.proxy_fallback_enabled, bool):
            raise ConfigurationFailure("proxy_fallback_enabled must be boolean")
        if (
            isinstance(self.max_proxy_switches, bool)
            or not isinstance(self.max_proxy_switches, int)
            or not 0 <= self.max_proxy_switches <= 3
        ):
            raise ConfigurationFailure("max_proxy_switches must be between 0 and 3")
        if self.proxy_fallback_enabled and self.max_proxy_switches == 0:
            raise ConfigurationFailure(
                "enabled proxy fallback requires at least one switch"
            )
        if not self.proxy_fallback_enabled and self.max_proxy_switches != 0:
            raise ConfigurationFailure(
                "disabled proxy fallback must use zero switches"
            )


@dataclass(frozen=True)
class AccessFailure:
    """访问限制、不可切换错误或切换耗尽后的结构化失败。"""

    error_type: ErrorType
    message: str
    status_code: Optional[int]
    retry_after_seconds: Optional[float]
    deferred: bool
    attempts: int
    proxy_switches: int
    last_error: TranslationPlatformError


@dataclass(frozen=True)
class AccessOutcome(Generic[ResultT]):
    """访问策略执行的成功值或结构化失败。"""

    value: Optional[ResultT]
    failure: Optional[AccessFailure]
    attempts: int
    proxy_switches: int

    @property
    def succeeded(self) -> bool:
        return self.failure is None


class AccessFallbackController:
    """先尊重 Retry-After，再按显式配置有限切换代理。"""

    def __init__(
        self,
        policy: AccessPolicy,
        switch_proxy: Callable[[TranslationPlatformError], bool],
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(policy, AccessPolicy):
            raise ConfigurationFailure("AccessPolicy is required")
        if not callable(switch_proxy) or not callable(now):
            raise ConfigurationFailure("switch_proxy and now must be callable")
        self._policy = policy
        self._switch_proxy = switch_proxy
        self._now = now

    def execute(self, operation: Callable[[], ResultT]) -> AccessOutcome[ResultT]:
        """执行有限次访问尝试；任何循环都受代理切换上限约束。"""

        if not callable(operation):
            raise ConfigurationFailure("access operation must be callable")
        switches = 0
        maximum_attempts = self._policy.max_proxy_switches + 1

        for attempt in range(1, maximum_attempts + 1):
            try:
                return AccessOutcome(
                    value=operation(),
                    failure=None,
                    attempts=attempt,
                    proxy_switches=switches,
                )
            except TranslationPlatformError as exc:
                retry_after = _retry_after_seconds(exc, self._now)
                if retry_after is not None:
                    return _failure_outcome(
                        error=exc,
                        attempts=attempt,
                        switches=switches,
                        retry_after=retry_after,
                        deferred=True,
                    )

                can_switch = (
                    self._policy.proxy_fallback_enabled
                    and _is_proxy_fallback_error(exc)
                    and switches < self._policy.max_proxy_switches
                )
                if can_switch and self._switch_proxy(exc) is True:
                    switches += 1
                    continue
                return _failure_outcome(
                    error=exc,
                    attempts=attempt,
                    switches=switches,
                    retry_after=None,
                    deferred=False,
                )

        raise AssertionError("finite access loop ended without an outcome")


def parse_retry_after(value: object, now: datetime) -> Optional[float]:
    """解析 Retry-After 的秒数或 HTTP 日期，无效值返回 ``None``。"""

    if not isinstance(now, datetime):
        raise ConfigurationFailure("now must return datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.isdigit():
        return float(normalized)
    try:
        target = parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if target is None:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0.0, (target - now).total_seconds())


def _retry_after_seconds(
    error: TranslationPlatformError,
    now: Callable[[], datetime],
) -> Optional[float]:
    if not isinstance(error, RateLimitError):
        return None
    return parse_retry_after(error.retry_after_raw, now=now())


def _is_proxy_fallback_error(error: TranslationPlatformError) -> bool:
    status_code = getattr(error, "status_code", None)
    return (
        isinstance(error, (AuthenticationFailure, TokenRefreshExhausted))
        and status_code == 401
    ) or (
        isinstance(error, AccessRestrictionError)
        and status_code == 403
    ) or (
        isinstance(error, RateLimitError)
        and status_code == 429
    )


def _failure_outcome(
    error: TranslationPlatformError,
    attempts: int,
    switches: int,
    retry_after: Optional[float],
    deferred: bool,
) -> AccessOutcome[object]:
    return AccessOutcome(
        value=None,
        failure=AccessFailure(
            error_type=error.error_type,
            message=str(error),
            status_code=getattr(error, "status_code", None),
            retry_after_seconds=retry_after,
            deferred=deferred,
            attempts=attempts,
            proxy_switches=switches,
            last_error=error,
        ),
        attempts=attempts,
        proxy_switches=switches,
    )
