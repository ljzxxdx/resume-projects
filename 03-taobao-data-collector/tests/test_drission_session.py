"""Tests for DrissionPage browser construction and ownership cleanup."""

from __future__ import annotations

import importlib
import unittest
from pathlib import Path

from taobao_collector.config import AppConfig


class FakeOptions:
    def __init__(self) -> None:
        self.port = None
        self.browser_path = None
        self.user_data_path = None
        self.headless_value = None
        self.arguments = []

    def set_local_port(self, port: int) -> "FakeOptions":
        self.port = port
        return self

    def set_user_data_path(self, path: str) -> "FakeOptions":
        self.user_data_path = path
        return self

    def set_browser_path(self, path: str) -> "FakeOptions":
        self.browser_path = path
        return self

    def headless(self, value: bool) -> "FakeOptions":
        self.headless_value = value
        return self

    def set_argument(self, argument: str) -> "FakeOptions":
        self.arguments.append(argument)
        return self


class FakeTab:
    def __init__(self, tab_id: str) -> None:
        self.tab_id = tab_id


class FakeBrowser:
    def __init__(
        self,
        quit_error: Exception = None,
        close_tabs_error: Exception = None,
        tab_ids=None,
        latest_tab_id: str = None,
    ) -> None:
        self.quit_count = 0
        self.quit_error = quit_error
        self.close_tabs_error = close_tabs_error
        self.tab_ids = list(tab_ids or ("main",))
        self.latest_tab = FakeTab(latest_tab_id or self.tab_ids[-1])
        self.closed_tab_ids = []

    def close_tabs(self, tabs_or_ids, others: bool = False) -> None:
        if self.close_tabs_error is not None:
            raise self.close_tabs_error
        keep_or_close_id = getattr(tabs_or_ids, "tab_id", tabs_or_ids)
        if others:
            targets = [
                tab_id
                for tab_id in self.tab_ids
                if tab_id != keep_or_close_id
            ]
        else:
            targets = [keep_or_close_id]
        self.closed_tab_ids.extend(targets)
        self.tab_ids = [
            tab_id for tab_id in self.tab_ids if tab_id not in targets
        ]

    def quit(self) -> None:
        self.quit_count += 1
        if self.quit_error is not None:
            raise self.quit_error


def make_config() -> AppConfig:
    return AppConfig(
        browser_port=9333,
        browser_path=Path("browser/msedge.exe"),
        user_data_dir=Path("profiles/test-user"),
        headless=True,
        default_wait=12.0,
        max_retries=2,
        log_level="INFO",
        screenshot_dir=Path("artifacts/screenshots"),
    )


class DrissionSessionTests(unittest.TestCase):
    def load_module(self):
        try:
            return importlib.import_module(
                "taobao_collector.collectors.drission_session"
            )
        except ModuleNotFoundError:
            self.fail("drission_session 模块尚未实现")

    def test_browser_options_map_typed_application_config(self) -> None:
        module = self.load_module()

        options = module.build_drission_options(
            make_config(),
            options_factory=FakeOptions,
        )

        self.assertEqual(options.port, 9333)
        self.assertEqual(
            options.browser_path,
            str(Path("browser/msedge.exe")),
        )
        self.assertEqual(
            options.user_data_path,
            str(Path("profiles/test-user")),
        )
        self.assertTrue(options.headless_value)
        self.assertIn("--profile-directory=Default", options.arguments)
        self.assertIn("--restore-last-session", options.arguments)

    def test_owned_browser_closes_restored_tabs_and_keeps_latest(self) -> None:
        module = self.load_module()
        browser = FakeBrowser(
            tab_ids=("old-home", "latest", "old-search"),
            latest_tab_id="latest",
        )
        session = module.DrissionBrowserSession(
            make_config(),
            browser_factory=lambda config: browser,
        )

        with session as active_browser:
            self.assertIs(active_browser, browser)
            self.assertEqual(browser.tab_ids, ["latest"])

        self.assertCountEqual(
            browser.closed_tab_ids,
            ["old-home", "old-search"],
        )
        self.assertEqual(browser.quit_count, 1)

    def test_restored_tab_cleanup_failure_quits_owned_browser(self) -> None:
        module = self.load_module()
        browser = FakeBrowser(
            close_tabs_error=RuntimeError("历史标签清理失败"),
            tab_ids=("latest", "old-search"),
            latest_tab_id="latest",
        )
        session = module.DrissionBrowserSession(
            make_config(),
            browser_factory=lambda config: browser,
        )

        with self.assertRaisesRegex(RuntimeError, "历史标签清理失败"):
            with session:
                pass

        self.assertEqual(browser.quit_count, 1)

    def test_owned_browser_quits_after_normal_exit(self) -> None:
        module = self.load_module()
        browser = FakeBrowser()
        session = module.DrissionBrowserSession(
            make_config(),
            browser_factory=lambda config: browser,
        )

        with session as active_browser:
            self.assertIs(active_browser, browser)

        self.assertEqual(browser.quit_count, 1)

    def test_owned_browser_quits_after_exception_and_interrupt(self) -> None:
        module = self.load_module()

        for exception in (RuntimeError("失败"), KeyboardInterrupt()):
            browser = FakeBrowser()
            session = module.DrissionBrowserSession(
                make_config(),
                browser_factory=lambda config, item=browser: item,
            )
            with self.subTest(exception=type(exception).__name__):
                with self.assertRaises(type(exception)):
                    with session:
                        raise exception
                self.assertEqual(browser.quit_count, 1)

    def test_injected_browser_keeps_caller_ownership(self) -> None:
        module = self.load_module()
        browser = FakeBrowser()
        session = module.DrissionBrowserSession(
            make_config(),
            browser=browser,
        )

        with session as active_browser:
            self.assertIs(active_browser, browser)

        self.assertEqual(browser.quit_count, 0)

    def test_quit_error_does_not_mask_keyboard_interrupt(self) -> None:
        browser = FakeBrowser(RuntimeError("浏览器退出失败"))
        session = self.load_module().DrissionBrowserSession(
            make_config(),
            browser_factory=lambda config: browser,
        )

        with self.assertRaises(KeyboardInterrupt):
            with session:
                raise KeyboardInterrupt()

        self.assertEqual(browser.quit_count, 1)

    def test_quit_error_is_reported_after_normal_body(self) -> None:
        browser = FakeBrowser(RuntimeError("浏览器退出失败"))
        session = self.load_module().DrissionBrowserSession(
            make_config(),
            browser_factory=lambda config: browser,
        )

        with self.assertRaisesRegex(RuntimeError, "浏览器退出失败"):
            with session:
                pass


if __name__ == "__main__":
    unittest.main()
