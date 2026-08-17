"""AI 翻译平台的不可变运行配置。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


SUPPORTED_LANGUAGES = ("en", "zh-CHS")


class ConfigurationError(ValueError):
    """配置值不符合运行边界时抛出。"""


def validate_language_pair(from_lang: str, to_lang: str) -> None:
    """校验源语言和目标语言是否受支持且互不相同。"""

    for language in (from_lang, to_lang):
        if language not in SUPPORTED_LANGUAGES:
            raise ConfigurationError(f"unsupported language: {language}")
    if from_lang == to_lang:
        raise ConfigurationError("source and target languages must differ")


@dataclass(frozen=True)
class RuntimeConfig:
    """一次运行使用的不可变、低负载配置。"""

    from_lang: str
    to_lang: str
    request_timeout_seconds: float = 30.0
    min_interval_seconds: float = 1.0
    max_retries: int = 3
    workers: int = 1
    use_proxy: bool = False
    proxy_reference: Optional[str] = None

    def __post_init__(self) -> None:
        validate_language_pair(self.from_lang, self.to_lang)
        _validate_positive_finite(
            "request_timeout_seconds",
            self.request_timeout_seconds,
        )
        _validate_number_range(
            "min_interval_seconds",
            self.min_interval_seconds,
            1,
            60,
        )
        _validate_integer_range("max_retries", self.max_retries, 0, 5)
        _validate_integer_range("workers", self.workers, 1, 2)

        if self.proxy_reference is not None:
            if not isinstance(self.proxy_reference, str):
                raise ConfigurationError("proxy reference must be text")
            normalized_reference = self.proxy_reference.strip()
            object.__setattr__(self, "proxy_reference", normalized_reference or None)
        if self.use_proxy and self.proxy_reference is None:
            raise ConfigurationError(
                "proxy reference is required when proxy use is enabled"
            )


def _validate_positive_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{name} must be positive and finite")


def _validate_number_range(
    name: str,
    value: float,
    minimum: float,
    maximum: float,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum:g} and {maximum:g}"
        )


def _validate_integer_range(
    name: str,
    value: int,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
