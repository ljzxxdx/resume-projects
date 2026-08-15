"""Selenium 浏览器创建与所有权清理。"""
from __future__ import annotations

from typing import Any, Callable, Optional

import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options

from taobao_collector.config import AppConfig


class SeleniumDriverSetupError(RuntimeError):
    """undetected-chromedriver 创建浏览器阶段失败。"""


def build_selenium_options(
    config: AppConfig,
    *,
    options_factory: Callable[[], Any] = Options,
) -> Any:
    """将应用配置映射为 Selenium 浏览器选项。"""

    options = options_factory()
    options.add_argument("--disable-blink-features=AutomationControlled")

    if config.browser_path is not None:
        options.binary_location = str(config.browser_path)
    if config.user_data_dir is not None:
        options.add_argument(
            f"--user-data-dir={config.user_data_dir}"
        )
        options.add_argument(
            "--profile-directory=Default"
        )
        options.add_argument(
            "--restore-last-session"
        )
    if config.headless:
        options.add_argument(
            "--headless=new"
        )
    return options


def build_selenium_driver(
    config: AppConfig,
    *,
    options_factory: Callable[[], Any] = Options,
    driver_factory: Callable[..., Any] = uc.Chrome,
) -> Any:
    """使用 undetected-chromedriver 创建并初始化 Chromium 会话。"""

    options = build_selenium_options(
        config,
        options_factory=options_factory,
    )

    driver_kwargs = {"options": options}
    if config.chrome_major_version is not None:
        driver_kwargs["version_main"] = config.chrome_major_version

    try:
        driver = driver_factory(**driver_kwargs)
    except Exception as exc:
        raise SeleniumDriverSetupError(
            "undetected-chromedriver 创建浏览器失败；"
            "请检查 Chrome 版本、网络连接和驱动缓存目录权限"
        ) from exc
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                })
            """
        })
    except Exception:
        try:
            driver.quit()
        except Exception:
            pass
        raise

    return driver


def _close_restored_tabs(driver: Any) -> None:
    """关闭恢复的历史标签，只保留启动时的当前标签。"""

    handles = tuple(driver.window_handles)
    if len(handles) <= 1:
        return

    current_handle = driver.current_window_handle
    if current_handle not in handles:
        current_handle = handles[-1]

    try:
        for handle in handles:
            if handle == current_handle:
                continue
            driver.switch_to.window(handle)
            driver.close()
    finally:
        if current_handle in driver.window_handles:
            driver.switch_to.window(current_handle)


class SeleniumBrowserSession:
    """管理 Selenium WebDriver 的创建与清理边界。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        driver: Optional[Any] = None,
        driver_factory: Callable[[AppConfig], Any] = (
            build_selenium_driver
        ),
    ) -> None:
        self.config = config
        self.driver_factory = driver_factory
        self.driver = driver
        self._owns_driver = driver is None

    def __enter__(self) -> Any:
        if self.driver is None:
            self.driver = self.driver_factory(self.config)
            try:
                _close_restored_tabs(self.driver)
            except BaseException:
                try:
                    self.driver.quit()
                except Exception:
                    pass
                raise

        return self.driver

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ) -> bool:
        if self._owns_driver and self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                if exc_type is None:
                    raise
        return False


__all__ = [
    "SeleniumBrowserSession",
    "SeleniumDriverSetupError",
    "build_selenium_driver",
    "build_selenium_options",
]
