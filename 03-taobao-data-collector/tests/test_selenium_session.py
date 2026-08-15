"""Tests for Selenium browser construction and ownership cleanup."""

from __future__ import annotations

import importlib
import unittest
from dataclasses import replace
from pathlib import Path

from taobao_collector.config import AppConfig


class FakeOptions:
    def __init__(self) -> None:
        self.binary_location = None
        self.arguments = []

    def add_argument(self, argument: str) -> None:
        self.arguments.append(argument)


class FakeSwitchTo:
    def __init__(self, driver: "FakeDriver") -> None:
        self.driver = driver

    def window(self, handle: str) -> None:
        if handle not in self.driver.window_handles:
            raise RuntimeError(f"窗口不存在：{handle}")
        self.driver.current_window_handle = handle


class FakeDriver:
    def __init__(
        self,
        quit_error: Exception | None = None,
        cdp_error: Exception | None = None,
        close_error: Exception | None = None,
        window_handles: tuple[str, ...] = ("main",),
        current_window_handle: str | None = None,
    ) -> None:
        self.quit_count = 0
        self.quit_error = quit_error
        self.cdp_error = cdp_error
        self.close_error = close_error
        self.cdp_calls = []
        self._window_handles = list(window_handles)
        self.current_window_handle = (
            current_window_handle or self._window_handles[-1]
        )
        self.closed_handles = []
        self.switch_to = FakeSwitchTo(self)

    @property
    def window_handles(self):
        return list(self._window_handles)

    def execute_cdp_cmd(self, command: str, parameters: dict) -> None:
        self.cdp_calls.append((command, parameters))
        if self.cdp_error is not None:
            raise self.cdp_error

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed_handles.append(self.current_window_handle)
        self._window_handles.remove(self.current_window_handle)

    def quit(self) -> None:
        self.quit_count += 1
        if self.quit_error is not None:
            raise self.quit_error


def make_config() -> AppConfig:
    return AppConfig(
        browser_port=9333,
        browser_path=Path("browser/chrome.exe"),
        user_data_dir=Path("profiles/selenium-user"),
        headless=True,
        default_wait=12.0,
        max_retries=2,
        log_level="INFO",
        screenshot_dir=Path("artifacts/screenshots"),
    )


class SeleniumOptionsTests(unittest.TestCase):
    def load_module(self):
        try:
            return importlib.import_module(
                "taobao_collector.collectors.selenium_session"
            )
        except ModuleNotFoundError:
            self.fail("selenium_session 模块尚未实现")

    def test_browser_options_map_typed_application_config(self) -> None:
        module = self.load_module()

        options = module.build_selenium_options(
            make_config(),
            options_factory=FakeOptions,
        )

        self.assertEqual(
            options.binary_location,
            str(Path("browser/chrome.exe")),
        )
        self.assertIn(
            f"--user-data-dir={Path('profiles/selenium-user')}",
            options.arguments,
        )
        self.assertIn("--profile-directory=Default", options.arguments)
        self.assertIn("--restore-last-session", options.arguments)
        self.assertIn("--headless=new", options.arguments)

    def test_browser_options_keep_selected_chromium_argument_only(self) -> None:
        module = self.load_module()

        options = module.build_selenium_options(
            make_config(),
            options_factory=FakeOptions,
        )

        joined_arguments = "\n".join(options.arguments).lower()
        self.assertIn(
            "--disable-blink-features=automationcontrolled",
            joined_arguments,
        )
        forbidden_fragments = (
            "proxy-server",
            "user-agent",
        )
        for fragment in forbidden_fragments:
            with self.subTest(fragment=fragment):
                self.assertNotIn(fragment, joined_arguments)


class SeleniumDriverTests(unittest.TestCase):
    def test_configured_chrome_major_version_is_passed_to_driver(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )
        config = replace(
            make_config(),
            chrome_major_version=151,
        )
        driver = FakeDriver()
        captured = {}

        def driver_factory(**kwargs):
            captured.update(kwargs)
            return driver

        module.build_selenium_driver(
            config,
            options_factory=FakeOptions,
            driver_factory=driver_factory,
        )

        self.assertEqual(captured.get("version_main"), 151)

    def test_driver_creation_failure_has_actionable_context(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )

        def failing_driver_factory(**kwargs):
            raise RuntimeError("driver download failed")

        with self.assertRaisesRegex(
            RuntimeError,
            "undetected-chromedriver 创建浏览器失败",
        ) as raised:
            module.build_selenium_driver(
                make_config(),
                options_factory=FakeOptions,
                driver_factory=failing_driver_factory,
            )

        self.assertEqual(
            raised.exception.__class__.__name__,
            "SeleniumDriverSetupError",
        )
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertEqual(
            str(raised.exception.__cause__),
            "driver download failed",
        )

    def test_driver_is_created_and_initialized_with_options(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )
        driver_builder = getattr(
            module,
            "build_selenium_driver",
            None,
        )
        self.assertIsNotNone(
            driver_builder,
            "build_selenium_driver 尚未实现",
        )
        expected_options = FakeOptions()
        expected_driver = FakeDriver()
        captured = {}

        def driver_factory(*, options):
            captured["options"] = options
            return expected_driver

        driver = driver_builder(
            make_config(),
            options_factory=lambda: expected_options,
            driver_factory=driver_factory,
        )

        self.assertIs(driver, expected_driver)
        self.assertIs(captured["options"], expected_options)
        self.assertEqual(len(expected_driver.cdp_calls), 1)
        command, parameters = expected_driver.cdp_calls[0]
        self.assertEqual(command, "Page.addScriptToEvaluateOnNewDocument")
        self.assertIsInstance(parameters.get("source"), str)
        self.assertTrue(parameters["source"].strip())

    def test_initialization_failure_quits_created_driver(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )
        driver = FakeDriver(cdp_error=RuntimeError("CDP 初始化失败"))

        with self.assertRaisesRegex(RuntimeError, "CDP 初始化失败"):
            module.build_selenium_driver(
                make_config(),
                options_factory=FakeOptions,
                driver_factory=lambda **kwargs: driver,
            )

        self.assertEqual(driver.quit_count, 1)

    def test_cleanup_failure_does_not_mask_initialization_failure(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )
        driver = FakeDriver(
            cdp_error=RuntimeError("CDP 初始化失败"),
            quit_error=RuntimeError("浏览器退出失败"),
        )

        with self.assertRaisesRegex(RuntimeError, "CDP 初始化失败"):
            module.build_selenium_driver(
                make_config(),
                options_factory=FakeOptions,
                driver_factory=lambda **kwargs: driver,
            )

        self.assertEqual(driver.quit_count, 1)


class SeleniumSessionTests(unittest.TestCase):
    def test_owned_driver_closes_restored_tabs_and_keeps_current(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )
        driver = FakeDriver(
            window_handles=("old-home", "current", "old-search"),
            current_window_handle="current",
        )
        session = module.SeleniumBrowserSession(
            make_config(),
            driver_factory=lambda config: driver,
        )

        with session as active_driver:
            self.assertIs(active_driver, driver)
            self.assertEqual(driver.window_handles, ["current"])
            self.assertEqual(driver.current_window_handle, "current")

        self.assertCountEqual(
            driver.closed_handles,
            ["old-home", "old-search"],
        )
        self.assertEqual(driver.quit_count, 1)

    def test_restored_tab_cleanup_failure_quits_owned_driver(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )
        driver = FakeDriver(
            close_error=RuntimeError("历史标签清理失败"),
            window_handles=("current", "old-search"),
            current_window_handle="current",
        )
        session = module.SeleniumBrowserSession(
            make_config(),
            driver_factory=lambda config: driver,
        )

        with self.assertRaisesRegex(RuntimeError, "历史标签清理失败"):
            with session:
                pass

        self.assertEqual(driver.quit_count, 1)

    def test_owned_driver_quits_after_normal_exit(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )
        session_class = getattr(
            module,
            "SeleniumBrowserSession",
            None,
        )
        self.assertIsNotNone(
            session_class,
            "SeleniumBrowserSession 尚未实现",
        )
        driver = FakeDriver()
        session = session_class(
            make_config(),
            driver_factory=lambda config: driver,
        )

        with session as active_driver:
            self.assertIs(active_driver, driver)

        self.assertEqual(driver.quit_count, 1)

    def test_injected_driver_keeps_caller_ownership(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )
        driver = FakeDriver(
            window_handles=("caller-main", "caller-secondary"),
            current_window_handle="caller-secondary",
        )
        try:
            session = module.SeleniumBrowserSession(
                make_config(),
                driver=driver,
            )
        except TypeError as exc:
            self.fail(f"会话类尚未支持外部 driver 注入：{exc}")

        with session as active_driver:
            self.assertIs(active_driver, driver)
            self.assertEqual(
                driver.window_handles,
                ["caller-main", "caller-secondary"],
            )

        self.assertEqual(driver.quit_count, 0)

    def test_quit_error_does_not_mask_body_exception(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )
        driver = FakeDriver(RuntimeError("浏览器退出失败"))
        session = module.SeleniumBrowserSession(
            make_config(),
            driver_factory=lambda config: driver,
        )

        with self.assertRaisesRegex(ValueError, "采集过程失败"):
            with session:
                raise ValueError("采集过程失败")

        self.assertEqual(driver.quit_count, 1)

    def test_quit_error_is_raised_after_normal_body(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_session"
        )
        driver = FakeDriver(RuntimeError("浏览器退出失败"))
        session = module.SeleniumBrowserSession(
            make_config(),
            driver_factory=lambda config: driver,
        )

        with self.assertRaisesRegex(RuntimeError, "浏览器退出失败"):
            with session:
                pass

        self.assertEqual(driver.quit_count, 1)


if __name__ == "__main__":
    unittest.main()
