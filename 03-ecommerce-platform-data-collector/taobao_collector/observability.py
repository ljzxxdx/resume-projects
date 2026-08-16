"""阻塞页面证据、截图路径和运行摘要持久化。"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from taobao_collector import selectors
from taobao_collector.collectors.base import CollectionBlockedError
from taobao_collector.models import Engine, RunSummary
from taobao_collector.verification import (
    VerificationContext,
    VerificationHandler,
)


_URL_BLOCK_SIGNALS = {
    "login.taobao.com": "进入淘宝登录页面，登录状态可能失效",
    "passport.taobao.com": "进入账号登录页面，登录状态可能失效",
    "captcha": "进入验证码页面",
    "punish": "进入访问限制页面",
    "sec.taobao.com": "进入安全验证页面",
}
_TEXT_BLOCK_SIGNALS = {
    "滑块验证": "检测到滑块验证",
    "拖动下方滑块": "检测到滑块验证",
    "安全验证": "检测到安全验证",
    "访问受限": "检测到访问受限提示",
    "验证码": "检测到验证码提示",
    "账号登录": "检测到账号登录页面",
}
_LOGIN_URL_SIGNALS = (
    "login.taobao.com",
    "passport.taobao.com",
)
_LOGIN_TEXT_SIGNALS = (
    "账号登录",
    "扫码登录",
    "亲，请登录",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_segment(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "_", value.strip())
    return normalized.strip("_") or "unknown"


def _page_url(page: Any) -> str:
    """兼容 Selenium WebDriver 与 DrissionPage Tab 的 URL 属性。"""
    if hasattr(page, "current_url"):
        return str(getattr(page, "current_url", "") or "")
    return str(getattr(page, "url", "") or "")


def _page_text(tab: Any) -> str:
    """读取两类浏览器页面的标题与正文文本。"""
    title = str(getattr(tab, "title", "") or "")
    if callable(getattr(tab, "find_element", None)):
        try:
            body_element = tab.find_element(
                *selectors.PAGE_BODY.selenium_locator
            )
        except Exception:
            body_element = None
    else:
        try:
            body_element = tab.ele(
                selectors.PAGE_BODY.drission_locator,
                timeout=0,
            )
        except Exception:
            body_element = None
    try:
        body_text = (
            str(getattr(body_element, "text", "") or "")
            if body_element is not None
            else ""
        )
    except Exception:
        body_text = ""
    return f"{title}\n{body_text}"


def _save_screenshot(page: Any, path: Path) -> None:
    """使用当前浏览器对象支持的标准接口保存截图。"""
    save_screenshot = getattr(page, "save_screenshot", None)
    if callable(save_screenshot):
        if save_screenshot(str(path)) is False:
            raise RuntimeError(f"Selenium 截图保存失败：{path}")
        return
    page.get_screenshot(path=str(path), full_page=True)


def _detect_non_login_reason(tab: Any) -> Optional[str]:
    url = _page_url(tab).lower()
    for signal, reason in _URL_BLOCK_SIGNALS.items():
        if signal not in _LOGIN_URL_SIGNALS and signal in url:
            return reason

    page_text = _page_text(tab)
    for signal, reason in _TEXT_BLOCK_SIGNALS.items():
        if signal not in {"账号登录", "验证码"} and signal in page_text:
            return reason
    return None


class ScreenshotStore:
    """按运行、引擎和步骤生成不可覆盖的截图路径。"""

    def __init__(
        self,
        root: Path,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.root = Path(root)
        self.clock = clock
        self._paths_by_run: Dict[str, List[Path]] = {}

    def capture(
        self,
        tab: Any,
        *,
        run_id: str,
        engine: Union[Engine, str],
        step: str,
    ) -> Path:
        engine_value = (
            engine.value if isinstance(engine, Engine) else str(engine)
        )
        directory = (
            self.root
            / _safe_segment(run_id)
            / _safe_segment(engine_value)
            / _safe_segment(step)
        )
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = self.clock().astimezone(timezone.utc).strftime(
            "%Y%m%dT%H%M%S%fZ"
        )
        path = directory / f"{timestamp}.png"
        suffix = 1
        known_paths = set(self._paths_by_run.get(run_id, ()))
        while path.exists() or path in known_paths:
            path = directory / f"{timestamp}-{suffix:03d}.png"
            suffix += 1
        _save_screenshot(tab, path)
        self._paths_by_run.setdefault(run_id, []).append(path)
        return path

    def paths_for(self, run_id: str) -> Tuple[Path, ...]:
        """返回本实例为指定运行成功保存的截图路径。"""

        return tuple(self._paths_by_run.get(run_id, ()))


class BlockGuard:
    """识别阻塞信号，并优先交给可选处理器尝试恢复。"""

    def __init__(
        self,
        screenshot_store: ScreenshotStore,
        *,
        verification_handler: Optional[VerificationHandler] = None,
    ) -> None:
        self.screenshot_store = screenshot_store
        self.verification_handler = verification_handler

    def inspect(
        self,
        tab: Any,
        *,
        run_id: str,
        engine: Union[Engine, str],
        step: str,
    ) -> None:
        reason = self._detect_reason(tab)
        if reason is None:
            return None

        if self.verification_handler is not None:
            context = VerificationContext(
                run_id=run_id,
                engine=engine,
                step=step,
                reason=reason,
            )
            self.verification_handler.handle(
                tab,
                context=context,
                is_blocked=lambda: self._detect_reason(tab) is not None,
            )
            reason = self._detect_reason(tab)
            if reason is None:
                return None

        screenshot_path = self.screenshot_store.capture(
            tab,
            run_id=run_id,
            engine=engine,
            step=step,
        )
        raise CollectionBlockedError(
            step=step,
            reason=reason,
            screenshot_path=screenshot_path,
        )

    @staticmethod
    def _detect_reason(tab: Any) -> Optional[str]:
        url = _page_url(tab).lower()
        for signal, reason in _URL_BLOCK_SIGNALS.items():
            if signal in url:
                return reason

        page_text = _page_text(tab)
        for signal, reason in _TEXT_BLOCK_SIGNALS.items():
            if signal in page_text:
                return reason
        return None


class ManualLoginWaiter:
    """登录页出现时等待用户在同一浏览器配置中完成登录。"""

    def __init__(
        self,
        *,
        timeout: float,
        poll_interval: float,
        screenshot_store: Optional[ScreenshotStore] = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        notifier: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.screenshot_store = screenshot_store
        self.clock = clock
        self.sleeper = sleeper
        self.notifier = notifier or (
            lambda message: print(message, file=sys.stderr)
        )

    @staticmethod
    def _is_login_page(tab: Any) -> bool:
        url = _page_url(tab).lower()
        if any(signal in url for signal in _LOGIN_URL_SIGNALS):
            return True
        page_text = _page_text(tab)
        return any(signal in page_text for signal in _LOGIN_TEXT_SIGNALS)

    def wait(
        self,
        tab: Any,
        *,
        run_id: str,
        engine: Union[Engine, str],
        step: str,
    ) -> None:
        if not self._is_login_page(tab):
            return

        self.notifier(
            "检测到淘宝登录页面，请在浏览器中完成登录；"
            "登录成功后程序会自动继续。"
        )
        deadline = self.clock() + self.timeout
        while self._is_login_page(tab):
            blocked_reason = _detect_non_login_reason(tab)
            if blocked_reason is not None:
                screenshot_path = self._capture(
                    tab,
                    run_id=run_id,
                    engine=engine,
                    step=step,
                )
                raise CollectionBlockedError(
                    step=step,
                    reason=blocked_reason,
                    screenshot_path=screenshot_path,
                )
            remaining = deadline - self.clock()
            if remaining <= 0:
                screenshot_path = self._capture(
                    tab,
                    run_id=run_id,
                    engine=engine,
                    step=f"{step}_login_timeout",
                )
                raise CollectionBlockedError(
                    step=step,
                    reason="等待人工登录超时",
                    screenshot_path=screenshot_path,
                )
            self.sleeper(min(self.poll_interval, remaining))

    def _capture(
        self,
        tab: Any,
        *,
        run_id: str,
        engine: Union[Engine, str],
        step: str,
    ) -> Optional[Path]:
        if self.screenshot_store is None:
            return None
        try:
            return self.screenshot_store.capture(
                tab,
                run_id=run_id,
                engine=engine,
                step=step,
            )
        except Exception:
            return None


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def configure_run_logging(
    output_dir: Path,
    *,
    run_id: str,
    level: str,
) -> logging.Logger:
    """创建同时写终端和独立 UTF-8 文件的运行日志器。"""

    level_value = getattr(logging, level.upper(), None)
    if not isinstance(level_value, int):
        raise ValueError(f"无效日志级别：{level}")

    logger = logging.getLogger(
        f"taobao_collector.run.{_safe_segment(run_id)}"
    )
    close_run_logging(logger)
    logger.setLevel(level_value)
    logger.propagate = False

    log_directory = Path(output_dir) / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    terminal_handler = logging.StreamHandler(sys.stderr)
    terminal_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(
        log_directory / f"{_safe_segment(run_id)}.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(terminal_handler)
    logger.addHandler(file_handler)
    return logger


def close_run_logging(logger: logging.Logger) -> None:
    """刷新并关闭当前运行拥有的日志处理器。"""

    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.flush()
        handler.close()


def write_run_summary(
    summary: RunSummary,
    output_dir: Path,
) -> Path:
    """将运行摘要写入独立运行目录。"""

    directory = Path(output_dir) / _safe_segment(summary.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "run_summary.json"
    payload = _json_value(asdict(summary))
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (directory / "screenshot_index.json").write_text(
        json.dumps(
            {
                "run_id": summary.run_id,
                "count": len(summary.screenshot_index),
                "screenshots": list(summary.screenshot_index),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


__all__ = [
    "BlockGuard",
    "close_run_logging",
    "configure_run_logging",
    "ManualLoginWaiter",
    "ScreenshotStore",
    "write_run_summary",
]
