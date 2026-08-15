"""Tests for blocked-page evidence and run summary persistence."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path

from taobao_collector import observability
from taobao_collector.collectors import base
from taobao_collector.models import (
    Engine,
    RecordStatus,
    RunMode,
    RunSummary,
)


class FakeBody:
    def __init__(self, text: str) -> None:
        self.text = text


class FakePage:
    def __init__(
        self,
        *,
        url: str,
        title: str = "",
        body: str = "",
    ) -> None:
        self.url = url
        self.title = title
        self.body = body
        self.screenshot_paths = []

    def ele(self, locator: str, *, timeout: float = 0):
        return FakeBody(self.body)

    def get_screenshot(
        self,
        *,
        path: str,
        full_page: bool,
    ) -> None:
        screenshot_path = Path(path)
        screenshot_path.write_bytes(b"fake-png")
        self.screenshot_paths.append(screenshot_path)


class MissingBodyPage(FakePage):
    """模拟页面刚开始导航、body 尚未进入 DOM 的 Drission Tab。"""

    def ele(self, locator: str, *, timeout: float = 0):
        raise LookupError(f"尚未找到元素：{locator}")


class DeferredMissingBodyPage(FakePage):
    """模拟 DrissionPage 返回NoneElement、读取text时才报错。"""

    class MissingElement:
        @property
        def text(self):
            raise LookupError("body尚未进入DOM")

    def ele(self, locator: str, *, timeout: float = 0):
        return self.MissingElement()


class FakeSeleniumPage:
    def __init__(
        self,
        *,
        url: str,
        title: str = "",
        body: str = "",
    ) -> None:
        self.current_url = url
        self.title = title
        self.body = body
        self.screenshot_paths = []

    def find_element(self, by: str, query: str):
        self.last_locator = (by, query)
        return FakeBody(self.body)

    def save_screenshot(self, path: str) -> bool:
        screenshot_path = Path(path)
        screenshot_path.write_bytes(b"fake-selenium-png")
        self.screenshot_paths.append(screenshot_path)
        return True


class ObservabilityTests(unittest.TestCase):
    def require(self, name: str):
        value = getattr(observability, name, None)
        self.assertIsNotNone(value, f"{name} 尚未实现")
        return value

    def make_guard(self, root: Path):
        store = self.require("ScreenshotStore")(
            root,
            clock=lambda: datetime(
                2026,
                7,
                28,
                1,
                2,
                3,
                tzinfo=timezone.utc,
            ),
        )
        return self.require("BlockGuard")(store)

    def test_login_url_saves_screenshot_and_raises_blocked_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = FakePage(
                url="https://login.taobao.com/member/login.jhtml",
                title="淘宝登录",
            )
            guard = self.make_guard(root)

            with self.assertRaises(base.CollectionBlockedError) as caught:
                guard.inspect(
                    page,
                    run_id="run-001",
                    engine=Engine.DRISSION,
                    step="product detail",
                )

            error = caught.exception
            self.assertEqual(error.step, "product detail")
            self.assertIn("登录页面", error.reason)
            self.assertTrue(error.screenshot_path.exists())
            self.assertEqual(
                error.screenshot_path.relative_to(root).parts[:3],
                ("run-001", "drission", "product_detail"),
            )
            self.assertEqual(len(page.screenshot_paths), 1)

    def test_captcha_text_is_blocked_but_normal_page_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            guard = self.make_guard(Path(directory))
            normal_page = FakePage(
                url="https://s.taobao.com/search?q=陶瓷",
                body="淘宝商品搜索结果",
            )

            for body in (
                "请完成滑块验证后继续访问",
                "亲，请拖动下方滑块完成验证",
            ):
                with self.subTest(body=body):
                    captcha_page = FakePage(
                        url="https://www.taobao.com/",
                        body=body,
                    )
                    with self.assertRaises(base.CollectionBlockedError):
                        guard.inspect(
                            captcha_page,
                            run_id="run-002",
                            engine=Engine.DRISSION,
                            step="search",
                        )

            self.assertIsNone(
                guard.inspect(
                    normal_page,
                    run_id="run-002",
                    engine=Engine.DRISSION,
                    step="search",
                )
            )
            self.assertEqual(normal_page.screenshot_paths, [])

    def test_block_guard_resumes_when_verification_handler_clears_page(
        self,
    ) -> None:
        class ResolvingHandler:
            def __init__(self) -> None:
                self.contexts = []

            def handle(self, page, *, context, is_blocked) -> bool:
                self.contexts.append(context)
                self.assertion_before_change = is_blocked()
                page.body = "淘宝商品搜索结果"
                return True

        with tempfile.TemporaryDirectory() as directory:
            store = self.require("ScreenshotStore")(Path(directory))
            handler = ResolvingHandler()
            guard = self.require("BlockGuard")(
                store,
                verification_handler=handler,
            )
            page = FakePage(
                url="https://www.taobao.com/",
                body="请拖动下方滑块完成验证",
            )

            result = guard.inspect(
                page,
                run_id="run-manual-resume",
                engine=Engine.DRISSION,
                step="product_results",
            )

            self.assertIsNone(result)
            self.assertTrue(handler.assertion_before_change)
            self.assertEqual(len(handler.contexts), 1)
            self.assertEqual(
                handler.contexts[0].reason,
                "检测到滑块验证",
            )
            self.assertEqual(page.screenshot_paths, [])

    def test_block_guard_still_captures_when_handler_cannot_clear_page(
        self,
    ) -> None:
        class UnresolvedHandler:
            def handle(self, page, *, context, is_blocked) -> bool:
                return False

        with tempfile.TemporaryDirectory() as directory:
            store = self.require("ScreenshotStore")(Path(directory))
            guard = self.require("BlockGuard")(
                store,
                verification_handler=UnresolvedHandler(),
            )
            page = FakeSeleniumPage(
                url="https://www.taobao.com/",
                body="请拖动下方滑块完成验证",
            )

            with self.assertRaises(base.CollectionBlockedError) as caught:
                guard.inspect(
                    page,
                    run_id="run-manual-timeout",
                    engine=Engine.SELENIUM,
                    step="product_results",
                )

            self.assertTrue(caught.exception.screenshot_path.exists())
            self.assertEqual(len(page.screenshot_paths), 1)

    def test_drission_missing_body_during_navigation_is_not_a_block(self) -> None:
        page = MissingBodyPage(url="https://www.taobao.com/")
        guard = self.make_guard(Path("artifacts/screenshots"))
        waiter = self.require("ManualLoginWaiter")(
            timeout=1,
            poll_interval=0.1,
            sleeper=lambda delay: None,
            notifier=lambda message: None,
        )

        waiter.wait(
            page,
            run_id="run-navigation",
            engine=Engine.DRISSION,
            step="home",
        )
        self.assertIsNone(
            guard.inspect(
                page,
                run_id="run-navigation",
                engine=Engine.DRISSION,
                step="home",
            )
        )

    def test_drission_none_element_text_failure_is_not_a_block(self) -> None:
        page = DeferredMissingBodyPage(url="https://www.taobao.com/")
        guard = self.make_guard(Path("artifacts/screenshots"))

        self.assertIsNone(
            guard.inspect(
                page,
                run_id="run-none-element",
                engine=Engine.DRISSION,
                step="home",
            )
        )

    def test_selenium_block_guard_reads_current_url_and_saves_screenshot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = FakeSeleniumPage(
                url="https://www.taobao.com/",
                title="安全验证",
                body="请完成滑块验证后继续访问",
            )
            guard = self.make_guard(root)

            with self.assertRaises(base.CollectionBlockedError) as caught:
                guard.inspect(
                    page,
                    run_id="run-selenium-blocked",
                    engine=Engine.SELENIUM,
                    step="live_detail",
                )

            self.assertIn("滑块验证", caught.exception.reason)
            self.assertTrue(caught.exception.screenshot_path.exists())
            self.assertEqual(
                caught.exception.screenshot_path.relative_to(root).parts[:3],
                ("run-selenium-blocked", "selenium", "live_detail"),
            )
            self.assertEqual(len(page.screenshot_paths), 1)

    def test_selenium_manual_login_waiter_resumes_after_url_changes(
        self,
    ) -> None:
        page = FakeSeleniumPage(
            url="https://login.taobao.com/member/login.jhtml"
        )
        sleeps = []

        def sleep(delay: float) -> None:
            sleeps.append(delay)
            page.current_url = "https://www.taobao.com/"

        waiter = self.require("ManualLoginWaiter")(
            timeout=180,
            poll_interval=0.5,
            sleeper=sleep,
            notifier=lambda message: None,
        )

        waiter.wait(
            page,
            run_id="run-selenium-login",
            engine=Engine.SELENIUM,
            step="home",
        )

        self.assertEqual(sleeps, [0.5])

    def test_manual_login_waiter_recognizes_logged_out_home_prompt(
        self,
    ) -> None:
        page = FakeSeleniumPage(
            url="https://www.taobao.com/",
            body="亲，请登录 免费注册 立即登录",
        )
        sleeps = []

        def sleep(delay: float) -> None:
            sleeps.append(delay)
            page.body = "我的淘宝"

        waiter = self.require("ManualLoginWaiter")(
            timeout=180,
            poll_interval=0.5,
            sleeper=sleep,
            notifier=lambda message: None,
        )

        waiter.wait(
            page,
            run_id="run-home-login",
            engine=Engine.SELENIUM,
            step="home",
        )

        self.assertEqual(sleeps, [0.5])

    def test_manual_login_waiter_resumes_after_login_page_leaves(self) -> None:
        page = FakePage(
            url="https://login.taobao.com/member/login.jhtml",
        )
        notices = []
        sleeps = []

        def sleep(delay: float) -> None:
            sleeps.append(delay)
            page.url = "https://www.taobao.com/"

        waiter = self.require("ManualLoginWaiter")(
            timeout=180,
            poll_interval=0.5,
            sleeper=sleep,
            notifier=notices.append,
        )

        waiter.wait(
            page,
            run_id="run-login",
            engine=Engine.DRISSION,
            step="home",
        )

        self.assertEqual(sleeps, [0.5])
        self.assertEqual(len(notices), 1)
        self.assertIn("完成登录", notices[0])

    def test_manual_login_waiter_does_not_delay_authenticated_page(self) -> None:
        page = FakePage(url="https://www.taobao.com/")
        sleeps = []
        waiter = self.require("ManualLoginWaiter")(
            timeout=180,
            poll_interval=0.5,
            sleeper=sleeps.append,
        )

        waiter.wait(
            page,
            run_id="run-login",
            engine=Engine.DRISSION,
            step="home",
        )

        self.assertEqual(sleeps, [])

    def test_manual_login_waiter_captures_timeout_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = FakePage(
                url="https://login.taobao.com/member/login.jhtml",
            )
            clock_values = iter((0.0, 0.4, 1.0))
            store = self.require("ScreenshotStore")(Path(directory))
            waiter = self.require("ManualLoginWaiter")(
                timeout=1.0,
                poll_interval=0.5,
                screenshot_store=store,
                clock=lambda: next(clock_values),
                sleeper=lambda delay: None,
            )

            with self.assertRaises(base.CollectionBlockedError) as caught:
                waiter.wait(
                    page,
                    run_id="run-login-timeout",
                    engine=Engine.DRISSION,
                    step="home",
                )

            self.assertIn("人工登录超时", caught.exception.reason)
            evidence_path = caught.exception.screenshot_path
            self.assertTrue(evidence_path.exists())
            self.assertEqual(
                evidence_path.relative_to(Path(directory)).parts[:3],
                (
                    "run-login-timeout",
                    "drission",
                    "home_login_timeout",
                ),
            )
            self.assertRegex(
                evidence_path.name,
                r"^\d{8}T\d{12}Z(?:-\d{3})?\.png$",
            )
            self.assertEqual(
                store.paths_for("run-login-timeout"),
                (evidence_path,),
            )

    def test_manual_login_waiter_stops_immediately_for_captcha(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = FakePage(
                url="https://login.taobao.com/member/login.jhtml",
                body="请完成滑块验证后继续登录",
            )
            sleeps = []
            store = self.require("ScreenshotStore")(Path(directory))
            waiter = self.require("ManualLoginWaiter")(
                timeout=180,
                poll_interval=0.5,
                screenshot_store=store,
                sleeper=sleeps.append,
            )

            with self.assertRaises(base.CollectionBlockedError) as caught:
                waiter.wait(
                    page,
                    run_id="run-login-captcha",
                    engine=Engine.DRISSION,
                    step="home",
                )

            self.assertIn("滑块验证", caught.exception.reason)
            self.assertEqual(sleeps, [])
            self.assertTrue(caught.exception.screenshot_path.exists())

    def test_manual_login_waiter_recognizes_login_page_text(self) -> None:
        page = FakePage(
            url="https://www.taobao.com/member",
            title="淘宝账号登录",
        )
        sleeps = []

        def sleep(delay: float) -> None:
            sleeps.append(delay)
            page.title = "我的淘宝"

        waiter = self.require("ManualLoginWaiter")(
            timeout=180,
            poll_interval=0.5,
            sleeper=sleep,
            notifier=lambda message: None,
        )

        waiter.wait(
            page,
            run_id="run-login-text",
            engine=Engine.DRISSION,
            step="home",
        )

        self.assertEqual(sleeps, [0.5])

    def test_manual_login_waiter_allows_sms_login_option_text(self) -> None:
        page = FakePage(
            url="https://login.taobao.com/member/login.jhtml",
            body="账号密码登录｜短信验证码登录",
        )
        sleeps = []

        def sleep(delay: float) -> None:
            sleeps.append(delay)
            page.url = "https://www.taobao.com/"
            page.body = "我的淘宝"

        waiter = self.require("ManualLoginWaiter")(
            timeout=180,
            poll_interval=0.5,
            sleeper=sleep,
            notifier=lambda message: None,
        )

        waiter.wait(
            page,
            run_id="run-sms-login",
            engine=Engine.DRISSION,
            step="home",
        )

        self.assertEqual(sleeps, [0.5])

    def test_blocked_run_summary_is_written_as_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = RunSummary(
                run_id="run-summary",
                engine=Engine.DRISSION,
                keyword="陶瓷",
                mode=RunMode.LIVE,
                ended_at=datetime(
                    2026,
                    7,
                    28,
                    2,
                    0,
                    tzinfo=timezone.utc,
                ),
                status=RecordStatus.BLOCKED,
                parameters={
                    "command": "live",
                    "live_limit": 2,
                },
                new_count=3,
                success_count=2,
                duplicate_count=1,
                missing_count=2,
                failed_count=1,
                screenshot_index=("screenshots/blocked.png",),
                error_message="检测到安全验证",
            )

            path = self.require("write_run_summary")(
                summary,
                Path(directory),
            )

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], "run-summary")
            self.assertEqual(payload["engine"], "drission")
            self.assertEqual(payload["mode"], "live")
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(
                payload["ended_at"],
                "2026-07-28T02:00:00+00:00",
            )
            self.assertEqual(payload["parameters"]["live_limit"], 2)
            self.assertEqual(payload["new_count"], 3)
            self.assertEqual(payload["success_count"], 2)
            self.assertEqual(payload["duplicate_count"], 1)
            self.assertEqual(payload["missing_count"], 2)
            self.assertEqual(
                payload["screenshot_index"],
                ["screenshots/blocked.png"],
            )
            screenshot_index = json.loads(
                (path.parent / "screenshot_index.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(screenshot_index["count"], 1)
            self.assertEqual(
                screenshot_index["screenshots"],
                ["screenshots/blocked.png"],
            )

    def test_screenshot_store_tracks_successful_captures_by_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.require("ScreenshotStore")(
                Path(directory),
                clock=lambda: datetime(
                    2026,
                    8,
                    1,
                    2,
                    3,
                    4,
                    tzinfo=timezone.utc,
                ),
            )
            page = FakePage(url="https://www.taobao.com/")

            path = store.capture(
                page,
                run_id="run-index",
                engine=Engine.DRISSION,
                step="missing_element",
            )

            self.assertEqual(store.paths_for("run-index"), (path,))
            self.assertEqual(store.paths_for("other-run"), ())

    def test_same_timestamp_screenshots_do_not_overwrite_each_other(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.require("ScreenshotStore")(
                Path(directory),
                clock=lambda: datetime(
                    2026,
                    8,
                    1,
                    2,
                    3,
                    4,
                    tzinfo=timezone.utc,
                ),
            )
            page = FakePage(url="https://www.taobao.com/")

            first = store.capture(
                page,
                run_id="run-collision",
                engine=Engine.DRISSION,
                step="timeout",
            )
            second = store.capture(
                page,
                run_id="run-collision",
                engine=Engine.DRISSION,
                step="timeout",
            )

            self.assertNotEqual(first, second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            self.assertEqual(
                store.paths_for("run-collision"),
                (first, second),
            )

    def test_standard_logger_writes_terminal_and_independent_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            terminal = io.StringIO()
            with redirect_stderr(terminal):
                logger = self.require("configure_run_logging")(
                    Path(directory),
                    run_id="run-log",
                    level="INFO",
                )
                logger.info("采集开始 run_id=%s", "run-log")
                self.require("close_run_logging")(logger)

            log_path = Path(directory) / "logs" / "run-log.log"
            file_text = log_path.read_text(encoding="utf-8")

        self.assertIn("采集开始", terminal.getvalue())
        self.assertIn("run-log", terminal.getvalue())
        self.assertIn("采集开始", file_text)
        self.assertIn("run-log", file_text)


if __name__ == "__main__":
    unittest.main()
