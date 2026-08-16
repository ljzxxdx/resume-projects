"""DrissionPage 浏览器创建与所有权清理。"""

from __future__ import annotations

from typing import Any, Callable, Optional

from DrissionPage import Chromium, ChromiumOptions

from taobao_collector.config import AppConfig


def build_drission_options(
    config: AppConfig,
    *,
    options_factory: Callable[[], Any] = ChromiumOptions,
) -> Any:
    """将应用配置映射为 DrissionPage 浏览器选项。"""

    options = options_factory()
    options.set_local_port(config.browser_port)
    if config.browser_path is not None:
        options.set_browser_path(str(config.browser_path))
    if config.user_data_dir is not None:
        options.set_user_data_path(str(config.user_data_dir))
        options.set_argument("--profile-directory=Default")
        options.set_argument("--restore-last-session")
    options.headless(config.headless)
    return options


def build_drission_browser(
    config: AppConfig,
    *,
    options_factory: Callable[[], Any] = ChromiumOptions,
    browser_factory: Callable[[Any], Any] = Chromium,
) -> Any:
    """根据应用配置创建 DrissionPage Chromium 会话。"""

    options = build_drission_options(
        config,
        options_factory=options_factory,
    )
    return browser_factory(options)


def _close_restored_tabs(browser: Any) -> None:
    """关闭恢复的历史标签，只保留 DrissionPage 最新标签。"""

    if len(tuple(browser.tab_ids)) <= 1:
        return
    browser.close_tabs(browser.latest_tab, others=True)


class DrissionBrowserSession:
    """只清理自身创建的浏览器，保留调用方注入会话。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        browser: Optional[Any] = None,
        browser_factory: Callable[[AppConfig], Any] = (
            build_drission_browser
        ),
    ) -> None:
        self.config = config
        self.browser = browser
        self.browser_factory = browser_factory
        self._owns_browser = browser is None

    def __enter__(self) -> Any:
        if self.browser is None:
            self.browser = self.browser_factory(self.config)
            try:
                _close_restored_tabs(self.browser)
            except BaseException:
                try:
                    self.browser.quit()
                except Exception:
                    pass
                raise
        return self.browser

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if self._owns_browser and self.browser is not None:
            try:
                self.browser.quit()
            except Exception:
                if exc_type is None:
                    raise
        return False


__all__ = [
    "DrissionBrowserSession",
    "build_drission_browser",
    "build_drission_options",
]
