"""由环境变量驱动的应用配置。"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from dotenv import load_dotenv


LOG_LEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


class ConfigError(ValueError):
    """表示环境配置缺失或格式无效。"""


def _parse_int(
    name: str,
    value: str,
    *,
    minimum: int,
    maximum: Optional[int] = None,
) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数") from exc
    if parsed < minimum:
        raise ConfigError(f"{name} 不能小于 {minimum}")
    if maximum is not None and parsed > maximum:
        raise ConfigError(f"{name} 不能大于 {maximum}")
    return parsed


def _parse_positive_float(name: str, value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是大于 0 的有限数字") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ConfigError(f"{name} 必须是大于 0 的有限数字")
    return parsed


def _parse_non_negative_float(name: str, value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是大于等于 0 的有限数字") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ConfigError(f"{name} 必须是大于等于 0 的有限数字")
    return parsed


def _parse_bool(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ConfigError(
        f"{name} 必须是 true/false、yes/no、on/off 或 1/0"
    )


def _parse_required_path(name: str, value: str) -> Path:
    normalized = value.strip()
    if not normalized:
        raise ConfigError(f"{name} 不能为空")
    return Path(normalized)


@dataclass(frozen=True)
class AppConfig:
    """浏览器、重试和可观测性运行配置。"""

    browser_port: int
    user_data_dir: Optional[Path]
    headless: bool
    default_wait: float
    max_retries: int
    log_level: str
    screenshot_dir: Path
    browser_path: Optional[Path] = None
    chrome_major_version: Optional[int] = None
    read_only_behavior: bool = True
    min_pause: float = 3.0
    max_pause: float = 6.0
    comment_min_pause: float = 3.0
    comment_max_pause: float = 5.0
    product_min_pause: float = 5.0
    product_max_pause: float = 8.0
    page_min_pause: float = 30.0
    page_max_pause: float = 60.0
    max_scrolls: int = 2
    max_tab_views: int = 2
    retry_base_delay: float = 10.0
    poll_interval: float = 0.05
    login_wait_timeout: float = 180.0
    verification_wait_timeout: float = 300.0
    live_behavior: bool = True
    live_min_pause: float = 6.0
    live_max_pause: float = 10.0
    live_room_min_pause: float = 8.0
    live_room_max_pause: float = 12.0
    live_max_scrolls: int = 2

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
    ) -> "AppConfig":
        # env["env"] = "sdadas"
        source = os.environ if env is None else env

        user_data_value = source.get(
            "TAOBAO_USER_DATA_DIR",
            "artifacts/chrome-profile",
        ).strip()
        browser_path_value = source.get(
            "TAOBAO_BROWSER_PATH",
            "",
        ).strip()
        chrome_major_version_value = source.get(
            "TAOBAO_CHROME_MAJOR_VERSION",
            "",
        ).strip()
        log_level = source.get(
            "TAOBAO_LOG_LEVEL",
            "INFO",
        ).strip().upper()
        if log_level not in LOG_LEVELS:
            allowed = ", ".join(sorted(LOG_LEVELS))
            raise ConfigError(
                f"TAOBAO_LOG_LEVEL 必须是以下值之一：{allowed}"
            )
        min_pause = _parse_non_negative_float(
            "TAOBAO_MIN_PAUSE",
            source.get("TAOBAO_MIN_PAUSE", "3"),
        )
        max_pause = _parse_non_negative_float(
            "TAOBAO_MAX_PAUSE",
            source.get("TAOBAO_MAX_PAUSE", "6"),
        )
        if max_pause < min_pause:
            raise ConfigError(
                "TAOBAO_MAX_PAUSE 不能小于 TAOBAO_MIN_PAUSE"
            )
        comment_min_pause = _parse_non_negative_float(
            "TAOBAO_COMMENT_MIN_PAUSE",
            source.get("TAOBAO_COMMENT_MIN_PAUSE", "3"),
        )
        comment_max_pause = _parse_non_negative_float(
            "TAOBAO_COMMENT_MAX_PAUSE",
            source.get("TAOBAO_COMMENT_MAX_PAUSE", "5"),
        )
        if comment_max_pause < comment_min_pause:
            raise ConfigError(
                "TAOBAO_COMMENT_MAX_PAUSE 不能小于 "
                "TAOBAO_COMMENT_MIN_PAUSE"
            )
        product_min_pause = _parse_non_negative_float(
            "TAOBAO_PRODUCT_MIN_PAUSE",
            source.get("TAOBAO_PRODUCT_MIN_PAUSE", "5"),
        )
        product_max_pause = _parse_non_negative_float(
            "TAOBAO_PRODUCT_MAX_PAUSE",
            source.get("TAOBAO_PRODUCT_MAX_PAUSE", "8"),
        )
        if product_max_pause < product_min_pause:
            raise ConfigError(
                "TAOBAO_PRODUCT_MAX_PAUSE 不能小于 "
                "TAOBAO_PRODUCT_MIN_PAUSE"
            )
        page_min_pause = _parse_non_negative_float(
            "TAOBAO_PAGE_MIN_PAUSE",
            source.get("TAOBAO_PAGE_MIN_PAUSE", "30"),
        )
        page_max_pause = _parse_non_negative_float(
            "TAOBAO_PAGE_MAX_PAUSE",
            source.get("TAOBAO_PAGE_MAX_PAUSE", "60"),
        )
        if page_max_pause < page_min_pause:
            raise ConfigError(
                "TAOBAO_PAGE_MAX_PAUSE 不能小于 "
                "TAOBAO_PAGE_MIN_PAUSE"
            )
        live_min_pause = _parse_non_negative_float(
            "TAOBAO_LIVE_MIN_PAUSE",
            source.get("TAOBAO_LIVE_MIN_PAUSE", "6"),
        )
        live_max_pause = _parse_non_negative_float(
            "TAOBAO_LIVE_MAX_PAUSE",
            source.get("TAOBAO_LIVE_MAX_PAUSE", "10"),
        )
        if live_max_pause < live_min_pause:
            raise ConfigError(
                "TAOBAO_LIVE_MAX_PAUSE 不能小于 "
                "TAOBAO_LIVE_MIN_PAUSE"
            )
        live_room_min_pause = _parse_non_negative_float(
            "TAOBAO_LIVE_ROOM_MIN_PAUSE",
            source.get("TAOBAO_LIVE_ROOM_MIN_PAUSE", "8"),
        )
        live_room_max_pause = _parse_non_negative_float(
            "TAOBAO_LIVE_ROOM_MAX_PAUSE",
            source.get("TAOBAO_LIVE_ROOM_MAX_PAUSE", "12"),
        )
        if live_room_max_pause < live_room_min_pause:
            raise ConfigError(
                "TAOBAO_LIVE_ROOM_MAX_PAUSE 不能小于 "
                "TAOBAO_LIVE_ROOM_MIN_PAUSE"
            )

        return cls(
            browser_port=_parse_int(
                "TAOBAO_BROWSER_PORT",
                source.get("TAOBAO_BROWSER_PORT", "9333"),
                minimum=1,
                maximum=65535,
            ),
            browser_path=(
                Path(browser_path_value)
                if browser_path_value
                else None
            ),
            chrome_major_version=(
                _parse_int(
                    "TAOBAO_CHROME_MAJOR_VERSION",
                    chrome_major_version_value,
                    minimum=1,
                )
                if chrome_major_version_value
                else None
            ),
            user_data_dir=(
                Path(user_data_value) if user_data_value else None
            ),
            headless=_parse_bool(
                "TAOBAO_HEADLESS",
                source.get("TAOBAO_HEADLESS", "false"),
            ),
            default_wait=_parse_positive_float(
                "TAOBAO_DEFAULT_WAIT",
                source.get("TAOBAO_DEFAULT_WAIT", "20"),
            ),
            max_retries=_parse_int(
                "TAOBAO_MAX_RETRIES",
                source.get("TAOBAO_MAX_RETRIES", "3"),
                minimum=0,
            ),
            log_level=log_level,
            screenshot_dir=_parse_required_path(
                "TAOBAO_SCREENSHOT_DIR",
                source.get(
                    "TAOBAO_SCREENSHOT_DIR",
                    "artifacts/screenshots",
                ),
            ),
            read_only_behavior=_parse_bool(
                "TAOBAO_READ_ONLY_BEHAVIOR",
                source.get("TAOBAO_READ_ONLY_BEHAVIOR", "true"),
            ),
            min_pause=min_pause,
            max_pause=max_pause,
            comment_min_pause=comment_min_pause,
            comment_max_pause=comment_max_pause,
            product_min_pause=product_min_pause,
            product_max_pause=product_max_pause,
            page_min_pause=page_min_pause,
            page_max_pause=page_max_pause,
            max_scrolls=_parse_int(
                "TAOBAO_MAX_SCROLLS",
                source.get("TAOBAO_MAX_SCROLLS", "2"),
                minimum=0,
            ),
            max_tab_views=_parse_int(
                "TAOBAO_MAX_TAB_VIEWS",
                source.get("TAOBAO_MAX_TAB_VIEWS", "2"),
                minimum=0,
            ),
            retry_base_delay=_parse_positive_float(
                "TAOBAO_RETRY_BASE_DELAY",
                source.get("TAOBAO_RETRY_BASE_DELAY", "10"),
            ),
            poll_interval=_parse_positive_float(
                "TAOBAO_POLL_INTERVAL",
                source.get("TAOBAO_POLL_INTERVAL", "0.05"),
            ),
            login_wait_timeout=_parse_positive_float(
                "TAOBAO_LOGIN_WAIT_TIMEOUT",
                source.get("TAOBAO_LOGIN_WAIT_TIMEOUT", "180"),
            ),
            verification_wait_timeout=_parse_positive_float(
                "TAOBAO_VERIFICATION_WAIT_TIMEOUT",
                source.get("TAOBAO_VERIFICATION_WAIT_TIMEOUT", "300"),
            ),
            live_behavior=_parse_bool(
                "TAOBAO_LIVE_BEHAVIOR",
                source.get("TAOBAO_LIVE_BEHAVIOR", "true"),
            ),
            live_min_pause=live_min_pause,
            live_max_pause=live_max_pause,
            live_room_min_pause=live_room_min_pause,
            live_room_max_pause=live_room_max_pause,
            live_max_scrolls=_parse_int(
                "TAOBAO_LIVE_MAX_SCROLLS",
                source.get("TAOBAO_LIVE_MAX_SCROLLS", "2"),
                minimum=0,
            ),
        )


def load_config(
    env_file: Optional[Path] = None,
) -> AppConfig:
    """读取 dotenv 文件和进程环境，并返回已校验配置。"""
    load_dotenv(dotenv_path=env_file, override=False)
    return AppConfig.from_env()
