"""默认关闭的私有代理配置、脱敏标识和有限故障切换。"""

from __future__ import annotations

import hashlib
import math
import os
import threading
import time
from collections.abc import Mapping, MutableMapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple
from unicodedata import category as unicode_category
from urllib.parse import urlsplit

from translation_platform.errors import (
    AccessRestrictionError,
    ConfigurationFailure,
    ErrorType,
    Http4xxError,
    NetworkConnectionError,
    RateLimitError,
)
from translation_platform.token import AuthenticationFailure, TokenRefreshExhausted


DEFAULT_ENVIRONMENT_VARIABLE = "TRANSLATION_PROXY_URLS"


@dataclass(frozen=True)
class ProxySettings:
    """仅在内存中保留完整 URL，对外只提供不可逆摘要。"""

    enabled: bool = False
    urls: Tuple[str, ...] = field(default=(), repr=False)
    max_switches: int = 0
    cooldown_seconds: float = 30.0
    access_status_fallback_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigurationFailure("proxy enabled flag must be boolean")
        if (
            isinstance(self.max_switches, bool)
            or not isinstance(self.max_switches, int)
            or not 0 <= self.max_switches <= 3
        ):
            raise ConfigurationFailure("proxy switches must be between 0 and 3")
        _validate_cooldown(self.cooldown_seconds)
        if not isinstance(self.access_status_fallback_enabled, bool):
            raise ConfigurationFailure(
                "access status fallback flag must be boolean"
            )
        if not self.enabled:
            if self.urls or self.max_switches or self.access_status_fallback_enabled:
                raise ConfigurationFailure(
                    "disabled proxy settings cannot contain private endpoints"
                )
            return
        if not self.urls:
            raise ConfigurationFailure("enabled proxy requires a private proxy source")
        if self.max_switches == 0:
            raise ConfigurationFailure("enabled proxy requires a finite switch budget")
        for value in self.urls:
            _validate_proxy_url(value)

    @classmethod
    def from_sources(
        cls,
        *,
        enabled: bool = False,
        environ: Optional[Mapping[str, str]] = None,
        private_config: Optional[Mapping[str, object]] = None,
        environment_variable: str = DEFAULT_ENVIRONMENT_VARIABLE,
        max_switches: int = 1,
        cooldown_seconds: float = 30.0,
        access_status_fallback_enabled: bool = False,
    ) -> "ProxySettings":
        """从专用环境变量或已读取的私有配置构造设置。"""

        if not isinstance(enabled, bool):
            raise ConfigurationFailure("proxy enabled flag must be boolean")
        if not enabled:
            return cls(
                enabled=False,
                max_switches=0,
                cooldown_seconds=cooldown_seconds,
                access_status_fallback_enabled=False,
            )
        if not isinstance(environment_variable, str) or not environment_variable.strip():
            raise ConfigurationFailure("proxy environment variable name is invalid")
        source_environment = os.environ if environ is None else environ
        if not isinstance(source_environment, Mapping):
            raise ConfigurationFailure("proxy environment source must be a mapping")

        candidates = []
        raw_environment = source_environment.get(environment_variable)
        if isinstance(raw_environment, str):
            candidates.extend(part.strip() for part in raw_environment.split(","))

        if private_config is not None:
            if not isinstance(private_config, Mapping):
                raise ConfigurationFailure("private proxy config must be a mapping")
            raw_urls = private_config.get("urls", ())
            if (
                isinstance(raw_urls, (str, bytes))
                or not isinstance(raw_urls, Sequence)
            ):
                raise ConfigurationFailure("private proxy URLs must be a sequence")
            candidates.extend(raw_urls)

        normalized = []
        for candidate in candidates:
            value = _validate_proxy_url(candidate)
            if value not in normalized:
                normalized.append(value)
        if not normalized:
            raise ConfigurationFailure("enabled proxy requires a private proxy source")
        return cls(
            enabled=True,
            urls=tuple(normalized),
            max_switches=max_switches,
            cooldown_seconds=float(cooldown_seconds),
            access_status_fallback_enabled=access_status_fallback_enabled,
        )

    @property
    def public_ids(self) -> Tuple[str, ...]:
        """返回不含主机、端口或认证信息的稳定摘要。"""

        return tuple(_public_proxy_id(url) for url in self.urls)


@dataclass(frozen=True)
class ProxyEvent:
    """可安全写入日志的代理状态事件，不包含完整出口信息。"""

    action: str
    proxy_id: str
    error_type: ErrorType


@dataclass(frozen=True)
class ProxyHealth:
    """一个代理出口的脱敏健康快照。"""

    proxy_id: str
    failure_count: int
    cooldown_remaining_seconds: float
    last_error_type: Optional[ErrorType]


class ProxyManager:
    """把代理应用到一个 Session，并有限冷却连接失败的出口。"""

    def __init__(
        self,
        settings: ProxySettings,
        session: object,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(settings, ProxySettings):
            raise ConfigurationFailure("ProxySettings is required")
        proxies = getattr(session, "proxies", None)
        if not isinstance(proxies, MutableMapping):
            raise ConfigurationFailure("session must provide a mutable proxies mapping")
        if not callable(monotonic):
            raise ConfigurationFailure("proxy monotonic clock must be callable")
        self._settings = settings
        self._session = session
        self._monotonic = monotonic
        self._current_index: Optional[int] = 0 if settings.enabled else None
        self._cooldown_until = {}
        self._failure_counts = {
            proxy_id: 0 for proxy_id in settings.public_ids
        }
        self._last_error_types = {
            proxy_id: None for proxy_id in settings.public_ids
        }
        self._switches = 0
        self._events = []
        self._lock = threading.RLock()
        self._apply_current()

    @property
    def current_proxy_id(self) -> Optional[str]:
        with self._lock:
            if self._current_index is None:
                return None
            return self._settings.public_ids[self._current_index]

    @property
    def switches(self) -> int:
        with self._lock:
            return self._switches

    @property
    def events(self) -> Tuple[ProxyEvent, ...]:
        """返回仅含摘要标识和分类原因的脱敏事件。"""

        with self._lock:
            return tuple(self._events)

    def is_bound_to(self, session: object) -> bool:
        """确认管理器只修改创建它时绑定的 Session。"""

        return session is self._session

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    @contextmanager
    def request_scope(self):
        """串行保护 Session 代理映射，并返回本次使用的脱敏标识。"""

        with self._lock:
            proxy_id = (
                None
                if self._current_index is None
                else self._settings.public_ids[self._current_index]
            )
            yield proxy_id

    def cooldown_remaining(self, proxy_id: Optional[str]) -> float:
        """按脱敏代理标识查询剩余冷却秒数。"""

        if proxy_id is None:
            return 0.0
        with self._lock:
            now = _read_monotonic(self._monotonic)
            return max(0.0, self._cooldown_until.get(proxy_id, 0.0) - now)

    def handle_connection_failure(
        self,
        error: object,
        proxy_id: Optional[str] = None,
    ) -> bool:
        """仅连接错误会冷却当前出口，并尝试一次受限切换。"""

        if not isinstance(error, NetworkConnectionError):
            return False
        return self.handle_failure(error, proxy_id=proxy_id)

    def handle_failure(
        self,
        error: object,
        proxy_id: Optional[str] = None,
    ) -> bool:
        """按显式健康策略记录失败，并在预算内选择可用出口。"""

        if not _is_health_failure(error, self._settings):
            return False
        with self._lock:
            if self._current_index is None:
                return False
            now = _read_monotonic(self._monotonic)
            current_id = self._settings.public_ids[self._current_index]
            attributed_id = (
                proxy_id
                if proxy_id is not None
                else getattr(error, "proxy_id_summary", None)
            )
            failed_id = current_id if attributed_id is None else attributed_id
            if failed_id not in self._failure_counts:
                return False
            self._failure_counts[failed_id] += 1
            self._last_error_types[failed_id] = error.error_type
            self._cooldown_until[failed_id] = now + self._settings.cooldown_seconds
            self._events.append(
                ProxyEvent(
                    action="cooldown",
                    proxy_id=failed_id,
                    error_type=error.error_type,
                )
            )
            if failed_id != current_id:
                return False
            if self._switches >= self._settings.max_switches:
                return False

            candidate = self._find_available_index(now)
            if candidate is None:
                return False
            self._current_index = candidate
            self._switches += 1
            self._apply_current()
            self._events.append(
                ProxyEvent(
                    action="switch",
                    proxy_id=self._settings.public_ids[candidate],
                    error_type=error.error_type,
                )
            )
            return True

    def mark_success(self, proxy_id: Optional[str] = None) -> None:
        """成功响应会清除本次出口的连续失败状态。"""

        with self._lock:
            if self._current_index is None:
                return
            current_id = self._settings.public_ids[self._current_index]
            succeeded_id = current_id if proxy_id is None else proxy_id
            if succeeded_id not in self._failure_counts:
                return
            self._failure_counts[succeeded_id] = 0
            self._last_error_types[succeeded_id] = None
            self._cooldown_until.pop(succeeded_id, None)

    def health_snapshot(self) -> Tuple[ProxyHealth, ...]:
        """返回不含 URL 和认证信息的当前健康状态。"""

        with self._lock:
            now = _read_monotonic(self._monotonic)
            return tuple(
                ProxyHealth(
                    proxy_id=proxy_id,
                    failure_count=self._failure_counts[proxy_id],
                    cooldown_remaining_seconds=max(
                        0.0,
                        self._cooldown_until.get(proxy_id, 0.0) - now,
                    ),
                    last_error_type=self._last_error_types[proxy_id],
                )
                for proxy_id in self._settings.public_ids
            )

    def _find_available_index(self, now: float) -> Optional[int]:
        if self._current_index is None:
            return None
        total = len(self._settings.urls)
        for offset in range(1, total + 1):
            candidate = (self._current_index + offset) % total
            candidate_id = self._settings.public_ids[candidate]
            if self._cooldown_until.get(candidate_id, 0.0) <= now:
                return candidate
        return None

    def _apply_current(self) -> None:
        proxies = self._session.proxies
        proxies.clear()
        setattr(self._session, "trust_env", False)
        if self._current_index is None:
            return
        url = self._settings.urls[self._current_index]
        proxies.update({"http": url, "https": url})


def _validate_proxy_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationFailure("private proxy URL is invalid")
    normalized = value.strip()
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
    except ValueError:
        raise ConfigurationFailure("private proxy URL is invalid") from None
    if (
        any(
            character.isspace() or unicode_category(character).startswith("C")
            for character in normalized
        )
        or
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or port is None
        or not 1 <= port <= 65535
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationFailure("private proxy URL is invalid")
    return normalized


def _public_proxy_id(url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return "proxy-" + digest


def _read_monotonic(clock: Callable[[], float]) -> float:
    value = clock()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ConfigurationFailure(
            "proxy monotonic clock must return a non-negative finite number"
        )
    return float(value)


def _validate_cooldown(value: object) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 1 <= value <= 3600
    ):
        raise ConfigurationFailure("proxy cooldown must be between 1 and 3600")


def _is_health_failure(error: object, settings: ProxySettings) -> bool:
    if isinstance(error, NetworkConnectionError):
        return True
    if not settings.access_status_fallback_enabled:
        return False
    status_code = getattr(error, "status_code", None)
    if isinstance(error, (AuthenticationFailure, TokenRefreshExhausted)):
        return status_code == 401
    if isinstance(error, AccessRestrictionError):
        return status_code == 403
    if isinstance(error, RateLimitError):
        return status_code == 429
    if isinstance(error, Http4xxError):
        return False
    return False
