"""统一的人工验证接管与本地滑块交互演示。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple, Union
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from taobao_collector.models import Engine


@dataclass(frozen=True)
class VerificationContext:
    """描述一次被页面验证阻塞的采集步骤。"""

    run_id: str
    engine: Union[Engine, str]
    step: str
    reason: str


class VerificationHandler(Protocol):
    """验证处理器共同遵循的最小接口。"""

    def handle(
        self,
        page: Any,
        *,
        context: VerificationContext,
        is_blocked: Callable[[], bool],
    ) -> bool:
        """已解除当前验证时返回 True，否则返回 False。"""


class SliderInteractor(Protocol):
    """针对具体浏览器引擎执行一次本地滑块拖动。"""

    def drag(self, page: Any) -> None:
        """把演示页滑块从起点拖到轨道末端。"""


class ManualVerificationHandler:
    """保持浏览器不关闭，等待用户手动完成页面验证。"""

    def __init__(
        self,
        *,
        timeout: float,
        poll_interval: float,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        notifier: Optional[Callable[[str], None]] = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout 必须大于 0")
        if poll_interval <= 0:
            raise ValueError("poll_interval 必须大于 0")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.clock = clock
        self.sleeper = sleeper
        self.notifier = notifier or print

    def handle(
        self,
        page: Any,
        *,
        context: VerificationContext,
        is_blocked: Callable[[], bool],
    ) -> bool:
        if "验证" not in context.reason:
            return False
        self.notifier(
            f"{context.reason}，请在当前浏览器中手动完成验证；"
            "验证消失后程序会自动继续。"
        )
        deadline = self.clock() + self.timeout
        while is_blocked():
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            self.sleeper(min(self.poll_interval, remaining))
        return True


def _page_url(page: Any) -> str:
    if hasattr(page, "current_url"):
        return str(getattr(page, "current_url", "") or "")
    return str(getattr(page, "url", "") or "")


def _is_authorized_local_url(url: str, allowed_file_root: Path) -> bool:
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return (parsed.hostname or "").lower() in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
    if parsed.scheme != "file":
        return False

    raw_path = url2pathname(unquote(parsed.path))
    candidate = Path(raw_path).resolve()
    root = allowed_file_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


class LocalSliderDemoHandler:
    """仅在本机授权页面调用对应引擎的滑块交互器。"""

    def __init__(
        self,
        *,
        interactors: Mapping[Engine, SliderInteractor],
        allowed_file_root: Path,
        timeout: float = 3.0,
        poll_interval: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interactors = dict(interactors)
        self.allowed_file_root = Path(allowed_file_root)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.clock = clock
        self.sleeper = sleeper

    def handle(
        self,
        page: Any,
        *,
        context: VerificationContext,
        is_blocked: Callable[[], bool],
    ) -> bool:
        if "滑块验证" not in context.reason:
            return False
        if not _is_authorized_local_url(
            _page_url(page),
            self.allowed_file_root,
        ):
            return False

        engine = (
            context.engine
            if isinstance(context.engine, Engine)
            else Engine(str(context.engine))
        )
        interactor = self.interactors.get(engine)
        if interactor is None:
            return False
        if not is_blocked():
            return True

        interactor.drag(page)
        deadline = self.clock() + self.timeout
        while is_blocked():
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            self.sleeper(min(self.poll_interval, remaining))
        return True


def _element_width(element: Any) -> float:
    size = getattr(element, "size", None)
    if isinstance(size, Mapping):
        width = size.get("width")
        if width is not None:
            return float(width)

    rect = getattr(element, "rect", None)
    rect_size = getattr(rect, "size", None)
    if isinstance(rect_size, Mapping):
        width = rect_size.get("width")
        if width is not None:
            return float(width)
    if isinstance(rect_size, (tuple, list)) and rect_size:
        return float(rect_size[0])
    raise ValueError("无法读取滑块元素宽度")


def _drag_distance(handle: Any, track: Any) -> float:
    distance = _element_width(track) - _element_width(handle)
    if distance <= 0:
        raise ValueError("滑块轨道宽度必须大于滑块宽度")
    return float(distance)


class SeleniumSliderInteractor:
    """使用 Selenium ActionChains 操作本地演示滑块。"""

    def __init__(
        self,
        *,
        handle_locator: Tuple[str, str],
        track_locator: Tuple[str, str],
        action_chain_factory: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        if action_chain_factory is None:
            from selenium.webdriver.common.action_chains import ActionChains

            action_chain_factory = ActionChains
        self.handle_locator = handle_locator
        self.track_locator = track_locator
        self.action_chain_factory = action_chain_factory

    def drag(self, page: Any) -> None:
        handle = page.find_element(*self.handle_locator)
        track = page.find_element(*self.track_locator)
        distance = _drag_distance(handle, track)
        (
            self.action_chain_factory(page)
            .move_to_element(handle)
            .click_and_hold()
            .move_by_offset(distance, 0)
            .release()
            .perform()
        )


class DrissionSliderInteractor:
    """使用 DrissionPage actions 操作本地演示滑块。"""

    def __init__(
        self,
        *,
        handle_locator: str,
        track_locator: str,
        duration: float = 0.8,
    ) -> None:
        self.handle_locator = handle_locator
        self.track_locator = track_locator
        self.duration = duration

    def drag(self, page: Any) -> None:
        handle = page.ele(self.handle_locator)
        track = page.ele(self.track_locator)
        distance = _drag_distance(handle, track)
        (
            page.actions.move_to(handle)
            .hold()
            .move(distance, 0, duration=self.duration)
            .release()
        )


__all__ = [
    "DrissionSliderInteractor",
    "LocalSliderDemoHandler",
    "ManualVerificationHandler",
    "SeleniumSliderInteractor",
    "SliderInteractor",
    "VerificationContext",
    "VerificationHandler",
]
