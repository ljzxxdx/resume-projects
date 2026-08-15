"""Selenium 异常分类、阻塞检查与截图上下文测试。"""

from __future__ import annotations

import importlib
import unittest
from contextlib import contextmanager
from pathlib import Path

from selenium.common.exceptions import (
    NoSuchElementException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)

from taobao_collector.collectors.base import (
    CollectionBlockedError,
    RetryPolicy,
)
from taobao_collector.models import Engine


class FakeDriver:
    current_url = "https://live.taobao.com/detail?roomId=9"


class RecordingScreenshotStore:
    def __init__(self) -> None:
        self.calls = []

    def capture(self, page, *, run_id, engine, step) -> Path:
        self.calls.append((page, run_id, engine, step))
        return Path(f"{step}.png")


class FailingScreenshotStore:
    def capture(self, page, *, run_id, engine, step) -> Path:
        raise OSError("截图目录不可写")


class GuardWithStore:
    def __init__(self, store, calls=None) -> None:
        self.screenshot_store = store
        self.calls = calls

    def inspect(self, page, *, run_id, engine, step) -> None:
        if self.calls is not None:
            self.calls.append(("block", step, engine))


class SeleniumFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )

    def test_login_waiter_runs_before_block_guard(self) -> None:
        calls = []

        class LoginWaiter:
            def wait(self, page, *, run_id, engine, step) -> None:
                calls.append(("login", step, engine))

        collector = self.module.SeleniumCollector(
            driver=FakeDriver(),
            run_id="run-selenium-check",
            login_waiter=LoginWaiter(),
            block_guard=GuardWithStore(None, calls),
        )

        collector._check_block("live_results")

        self.assertEqual(
            calls,
            [
                ("login", "live_results", Engine.SELENIUM),
                ("block", "live_results", Engine.SELENIUM),
            ],
        )

    def test_exception_types_are_classified_from_specific_to_general(
        self,
    ) -> None:
        classify = getattr(
            self.module.SeleniumCollector,
            "_classify_exception",
            None,
        )
        self.assertIsNotNone(classify, "_classify_exception 尚未实现")

        cases = (
            (TimeoutException(), "element_timeout"),
            (NoSuchElementException(), "selector_invalid"),
            (NoSuchWindowException(), "window_lost"),
            (WebDriverException(), "webdriver_error"),
            (RuntimeError(), "unexpected_error"),
        )
        for error, expected in cases:
            with self.subTest(error=type(error).__name__):
                self.assertEqual(classify(error), expected)

    def test_failure_capture_keeps_first_specific_context(self) -> None:
        store = RecordingScreenshotStore()
        collector = self.module.SeleniumCollector(
            driver=FakeDriver(),
            run_id="run-selenium-failure",
            block_guard=GuardWithStore(store),
        )

        collector._capture_failure_evidence(
            "live_detail",
            TimeoutException("商品标题等待超时"),
        )
        collector._capture_failure_evidence(
            "live_results",
            RuntimeError("外层重复捕获"),
        )

        self.assertEqual(store.calls, [
            (
                collector.driver,
                "run-selenium-failure",
                Engine.SELENIUM,
                "live_detail",
            )
        ])
        self.assertEqual(
            collector.last_failure_screenshot,
            Path("live_detail.png"),
        )
        self.assertEqual(
            collector.last_failure_context,
            {
                "url": "https://live.taobao.com/detail?roomId=9",
                "engine": "selenium",
                "step": "live_detail",
                "error_type": "element_timeout",
                "exception_type": "TimeoutException",
                "message": "Message: 商品标题等待超时\n",
                "screenshot_path": "live_detail.png",
            },
        )

    def test_failure_context_survives_missing_or_broken_screenshot_store(
        self,
    ) -> None:
        without_store = self.module.SeleniumCollector(
            driver=FakeDriver(),
            run_id="run-no-screenshot-store",
        )
        broken_store = self.module.SeleniumCollector(
            driver=FakeDriver(),
            run_id="run-broken-screenshot-store",
            block_guard=GuardWithStore(FailingScreenshotStore()),
        )

        without_store._capture_failure_evidence(
            "product_results",
            RuntimeError("没有截图仓库"),
        )
        broken_store._capture_failure_evidence(
            "live_results",
            RuntimeError("截图失败也要保留原异常"),
        )

        self.assertIsNone(without_store.last_failure_screenshot)
        self.assertIsNone(
            without_store.last_failure_context["screenshot_path"]
        )
        self.assertEqual(
            without_store.last_failure_context["step"],
            "product_results",
        )
        self.assertIsNone(broken_store.last_failure_screenshot)
        self.assertIsNone(
            broken_store.last_failure_context["screenshot_path"]
        )
        self.assertEqual(
            broken_store.last_failure_context["message"],
            "截图失败也要保留原异常",
        )

    def test_blocked_error_records_existing_block_screenshot(self) -> None:
        calls = []
        blocked_path = Path("blocked.png")

        class LoginWaiter:
            def wait(self, page, *, run_id, engine, step) -> None:
                calls.append("login")

        class BlockingGuard:
            screenshot_store = None

            def inspect(self, page, *, run_id, engine, step) -> None:
                calls.append("block")
                raise CollectionBlockedError(
                    step=step,
                    reason="检测到滑块验证",
                    screenshot_path=blocked_path,
                )

        collector = self.module.SeleniumCollector(
            driver=FakeDriver(),
            run_id="run-selenium-blocked",
            login_waiter=LoginWaiter(),
            block_guard=BlockingGuard(),
        )

        with self.assertRaises(CollectionBlockedError):
            collector._check_block("home")

        self.assertEqual(calls, ["login", "block"])
        self.assertEqual(collector.last_failure_screenshot, blocked_path)
        self.assertEqual(
            collector.last_failure_context["error_type"],
            "blocked",
        )
        self.assertEqual(collector.last_failure_context["step"], "home")
        self.assertEqual(
            collector.last_failure_context["screenshot_path"],
            "blocked.png",
        )

    def test_live_entry_failure_is_captured_before_any_tab_opens(self) -> None:
        store = RecordingScreenshotStore()

        class EntryFailureCollector(self.module.SeleniumCollector):
            def _open_home(self) -> None:
                raise WebDriverException("首页打开失败")

        collector = EntryFailureCollector(
            driver=FakeDriver(),
            run_id="run-live-entry-failure",
            block_guard=GuardWithStore(store),
        )

        with self.assertRaises(WebDriverException):
            collector.collect_live(keyword="陶瓷", live_limit=1)

        self.assertEqual(collector.last_failure_context["step"], "live_entry")
        self.assertEqual(
            collector.last_failure_context["error_type"],
            "webdriver_error",
        )
        self.assertEqual(store.calls[0][3], "live_entry")

    def test_live_results_and_detail_failures_keep_specific_step(self) -> None:
        module = self.module
        data_class = getattr(module, "_LiveCardData")
        navigation = object()
        detail_link = object()

        class Card:
            def find_element(self, by, query):
                return detail_link

        card = Card()

        class FailureDriver:
            current_url = "https://www.taobao.com/"

        class FlowFailureCollector(module.SeleniumCollector):
            def __init__(self, *, failure_step, store):
                super().__init__(
                    driver=FailureDriver(),
                    run_id=f"run-{failure_step}-failure",
                    block_guard=GuardWithStore(store),
                )
                self.failure_step = failure_step
                self.depth = 0

            def _open_home(self) -> None:
                pass

            def _wait_live_navigation(self):
                return navigation

            def _move_and_click(self, element) -> None:
                pass

            @contextmanager
            def _temporary_new_tab(self, opener):
                original_url = self.driver.current_url
                opener()
                self.depth += 1
                self.driver.current_url = (
                    "https://live/results"
                    if self.depth == 1
                    else "https://live/detail?roomId=9"
                )
                try:
                    yield f"tab-{self.depth}"
                finally:
                    self.depth -= 1
                    self.driver.current_url = original_url

            def _wait_live_search_input(self):
                if self.failure_step == "live_results":
                    raise NoSuchElementException("直播搜索框失效")
                return object()

            def _submit_live_search(self, search_input, keyword) -> None:
                pass

            def _load_live_cards(self, *, live_limit):
                return [card]

            @staticmethod
            def _parse_live_card(item):
                return data_class(
                    live_room_id="room-9",
                    source_url="https://live/detail?roomId=room-9",
                    account_name="主播",
                    introduction="介绍",
                    viewer_count=10,
                    follower_count=20,
                )

            def _wait_live_detail(self):
                raise TimeoutException("直播商品标题等待超时")

        cases = (
            ("live_results", NoSuchElementException),
            ("live_detail", TimeoutException),
        )
        for failure_step, error_class in cases:
            with self.subTest(failure_step=failure_step):
                store = RecordingScreenshotStore()
                collector = FlowFailureCollector(
                    failure_step=failure_step,
                    store=store,
                )

                with self.assertRaises(error_class):
                    collector.collect_live(keyword="陶瓷", live_limit=1)

                self.assertEqual(collector.depth, 0)
                self.assertEqual(
                    collector.last_failure_context["step"],
                    failure_step,
                )
                self.assertEqual(len(store.calls), 1)
                self.assertEqual(store.calls[0][3], failure_step)

    def test_product_results_failure_is_captured_and_rethrown(self) -> None:
        store = RecordingScreenshotStore()

        class ProductResultsFailureCollector(self.module.SeleniumCollector):
            def _open_home(self) -> None:
                pass

            def _wait_product_search_input(self):
                return object()

            def _submit_product_search(self, search_input, keyword) -> None:
                pass

            def _wait_product_cards(self):
                raise NoSuchElementException("商品列表选择器失效")

        collector = ProductResultsFailureCollector(
            driver=FakeDriver(),
            run_id="run-product-results-failure",
            block_guard=GuardWithStore(store),
        )

        with self.assertRaises(NoSuchElementException):
            collector.collect_products(
                keyword="陶瓷",
                product_limit=1,
                comment_limit=1,
            )

        self.assertEqual(
            collector.last_failure_context["step"],
            "product_results",
        )
        self.assertEqual(
            collector.last_failure_context["error_type"],
            "selector_invalid",
        )
        self.assertEqual(len(store.calls), 1)

    def test_product_detail_captures_only_after_retries_exhausted(self) -> None:
        module = self.module
        data_class = getattr(module, "_ProductCardData")
        store = RecordingScreenshotStore()
        product = data_class(
            product_id="retry-failure",
            source_url="https://item.taobao.com/item.htm?id=retry-failure",
            name="重试失败商品",
            price=None,
            sales_count=30,
        )

        class DetailFailureCollector(module.SeleniumCollector):
            def __init__(self):
                super().__init__(
                    driver=FakeDriver(),
                    run_id="run-product-detail-failure",
                    retry_policy=RetryPolicy(
                        max_retries=1,
                        base_delay=0.01,
                    ),
                    sleeper=lambda delay: None,
                    block_guard=GuardWithStore(store),
                )
                self.attempts = 0
                self.closed_scopes = 0

            @contextmanager
            def _temporary_new_tab(self, opener):
                opener()
                try:
                    yield "detail"
                finally:
                    self.closed_scopes += 1

            def _move_and_click(self, element) -> None:
                pass

            def _wait_product_detail(self):
                self.attempts += 1
                raise TimeoutException("商品详情等待超时")

        collector = DetailFailureCollector()

        with self.assertRaises(TimeoutException):
            collector._collect_product_with_retries(
                card=object(),
                product=product,
                keyword="陶瓷",
                comment_limit=1,
            )

        self.assertEqual(collector.attempts, 2)
        self.assertEqual(collector.closed_scopes, 2)
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(store.calls[0][3], "product_detail")
        self.assertEqual(
            collector.last_failure_context["step"],
            "product_detail",
        )


if __name__ == "__main__":
    unittest.main()
