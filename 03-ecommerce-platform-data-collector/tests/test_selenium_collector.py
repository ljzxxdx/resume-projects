"""Selenium 采集器显式等待行为测试。"""

from __future__ import annotations

import importlib
import unittest
from unittest.mock import patch

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By

from taobao_collector import selectors
from taobao_collector.collectors.base import (
    LiveBehaviorPolicy,
    ReadOnlyBehaviorPolicy,
    WaitPolicy,
)


class FakeVisibleElement:
    def is_displayed(self) -> bool:
        return True


class FakeClickableElement(FakeVisibleElement):
    def is_enabled(self) -> bool:
        return True


class FakeDisabledElement(FakeVisibleElement):
    def is_enabled(self) -> bool:
        return False


class LocatorDriver:
    def __init__(self, locator, element) -> None:
        self.locator = locator
        self.element = element

    def find_element(self, by, query):
        if (by, query) != self.locator:
            raise NoSuchElementException(
                f"未找到定位器：{(by, query)!r}"
            )
        return self.element


class LocatorListDriver:
    def __init__(self, locator, elements) -> None:
        self.locator = locator
        self.elements = elements

    def find_elements(self, by, query):
        if (by, query) != self.locator:
            return []
        return self.elements


class GrowingListDriver:
    def __init__(self, locator, snapshots) -> None:
        self.locator = locator
        self.snapshots = snapshots
        self.call_count = 0

    def find_elements(self, by, query):
        if (by, query) != self.locator:
            return []
        index = min(self.call_count, len(self.snapshots) - 1)
        self.call_count += 1
        return self.snapshots[index]


class EventuallyStaleElement:
    def __init__(self) -> None:
        self.check_count = 0

    def is_enabled(self) -> bool:
        self.check_count += 1
        if self.check_count >= 2:
            raise StaleElementReferenceException("旧元素已失效")
        return True


class FakeSwitchTo:
    def __init__(self, driver) -> None:
        self.driver = driver

    def window(self, handle: str) -> None:
        if handle not in self.driver.handles:
            raise RuntimeError(f"窗口不存在：{handle}")
        self.driver.current_handle = handle
        self.driver.switch_calls.append(handle)


class FakeWindowDriver:
    def __init__(
        self,
        *,
        handles=None,
        current_handle: str = "main",
        close_error_handles=None,
    ) -> None:
        self.handles = list(handles or [current_handle])
        self.current_handle = current_handle
        self.close_error_handles = set(close_error_handles or ())
        self.closed_handles = []
        self.switch_calls = []
        self.switch_to = FakeSwitchTo(self)

    @property
    def window_handles(self):
        return list(self.handles)

    @property
    def current_window_handle(self) -> str:
        return self.current_handle

    def open_window(self, handle: str) -> None:
        self.handles.append(handle)

    def close(self) -> None:
        handle = self.current_handle
        if handle not in self.handles:
            raise RuntimeError(f"窗口不存在：{handle}")
        self.handles.remove(handle)
        self.closed_handles.append(handle)
        self.current_handle = ""
        if handle in self.close_error_handles:
            raise RuntimeError(f"关闭窗口失败：{handle}")


class BehaviorElement:
    def __init__(self, name: str) -> None:
        self.name = name
        self.scroll_top = 0


class BehaviorDriver:
    def __init__(self, elements_by_locator=None) -> None:
        self.elements_by_locator = dict(elements_by_locator or {})
        self.action_batches = []
        self.script_scrolls = []

    def find_elements(self, by, query):
        return list(self.elements_by_locator.get((by, query), ()))

    def execute_script(self, script, element, distance) -> None:
        if "scrollTop" not in script:
            raise AssertionError(f"非预期脚本：{script}")
        element.scroll_top += distance
        self.script_scrolls.append((element, distance))


class RecordingActionChains:
    def __init__(self, driver) -> None:
        self.driver = driver
        self.actions = []

    def move_to_element(self, element):
        self.actions.append(("move", element))
        return self

    def click(self):
        self.actions.append(("click",))
        return self

    def scroll_by_amount(self, delta_x: int, delta_y: int):
        self.actions.append(("scroll", delta_x, delta_y))
        return self

    def perform(self) -> None:
        self.driver.action_batches.append(tuple(self.actions))


class PredictableRandom:
    def __init__(self, delay: float = 0.5) -> None:
        self.delay = delay
        self.uniform_bounds = []
        self.sample_sizes = []

    def uniform(self, minimum: float, maximum: float) -> float:
        self.uniform_bounds.append((minimum, maximum))
        return self.delay

    def sample(self, population, count: int):
        self.sample_sizes.append(count)
        return list(population[:count])


class SeleniumWaitTests(unittest.TestCase):
    def test_wait_visible_returns_displayed_search_input(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        collector_class = getattr(module, "SeleniumCollector", None)
        self.assertIsNotNone(
            collector_class,
            "SeleniumCollector 尚未实现",
        )
        element = FakeVisibleElement()
        driver = LocatorDriver(
            (By.CSS_SELECTOR, selectors.SEARCH_INPUT.query),
            element,
        )
        collector = collector_class(
            driver=driver,
            run_id="run-selenium-wait",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )

        actual = collector._wait_visible(selectors.SEARCH_INPUT)

        self.assertIs(actual, element)

    def test_wait_clickable_returns_enabled_visible_button(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        collector = module.SeleniumCollector(
            driver=LocatorDriver(
                (
                    By.CSS_SELECTOR,
                    selectors.COMMENT_OPEN_BUTTON.query,
                ),
                FakeClickableElement(),
            ),
            run_id="run-selenium-wait",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_clickable = getattr(collector, "_wait_clickable", None)
        self.assertIsNotNone(
            wait_clickable,
            "_wait_clickable 尚未实现",
        )

        actual = wait_clickable(selectors.COMMENT_OPEN_BUTTON)

        self.assertIs(actual, collector.driver.element)

    def test_wait_clickable_rejects_visible_disabled_button(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        collector = module.SeleniumCollector(
            driver=LocatorDriver(
                (
                    By.CSS_SELECTOR,
                    selectors.COMMENT_OPEN_BUTTON.query,
                ),
                FakeDisabledElement(),
            ),
            run_id="run-selenium-wait",
            wait_policy=WaitPolicy(
                timeout=0.01,
                poll_interval=0.001,
            ),
        )

        with self.assertRaises(TimeoutException):
            collector._wait_clickable(
                selectors.COMMENT_OPEN_BUTTON
            )

    def test_wait_all_present_returns_product_list(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        elements = [object(), object()]
        collector = module.SeleniumCollector(
            driver=LocatorListDriver(
                (
                    By.XPATH,
                    selectors.PRODUCT_LIST_ITEMS.query,
                ),
                elements,
            ),
            run_id="run-selenium-wait",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_all_present = getattr(
            collector,
            "_wait_all_present",
            None,
        )
        self.assertIsNotNone(
            wait_all_present,
            "_wait_all_present 尚未实现",
        )

        actual = wait_all_present(selectors.PRODUCT_LIST_ITEMS)

        self.assertEqual(actual, elements)

    def test_wait_for_more_elements_ignores_unchanged_list(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        initial = [object()]
        expanded = [initial[0], object()]
        collector = module.SeleniumCollector(
            driver=GrowingListDriver(
                (
                    By.XPATH,
                    selectors.COMMENT_ITEMS.query,
                ),
                [initial, expanded],
            ),
            run_id="run-selenium-wait",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_for_more = getattr(
            collector,
            "_wait_for_more_elements",
            None,
        )
        self.assertIsNotNone(
            wait_for_more,
            "_wait_for_more_elements 尚未实现",
        )

        actual = wait_for_more(
            selectors.COMMENT_ITEMS,
            previous_count=1,
        )

        self.assertEqual(actual, expanded)
        self.assertEqual(collector.driver.call_count, 2)

    def test_wait_until_stale_polls_until_old_card_detaches(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        collector = module.SeleniumCollector(
            driver=object(),
            run_id="run-selenium-wait",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_until_stale = getattr(
            collector,
            "_wait_until_stale",
            None,
        )
        self.assertIsNotNone(
            wait_until_stale,
            "_wait_until_stale 尚未实现",
        )
        old_card = EventuallyStaleElement()

        actual = wait_until_stale(old_card)

        self.assertTrue(actual)
        self.assertEqual(old_card.check_count, 2)

    def test_wait_product_cards_uses_registered_product_items(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        product_cards = [object(), object()]
        collector = module.SeleniumCollector(
            driver=LocatorListDriver(
                (
                    By.XPATH,
                    selectors.PRODUCT_LIST_ITEMS.query,
                ),
                product_cards,
            ),
            run_id="run-selenium-products",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_product_cards = getattr(
            collector,
            "_wait_product_cards",
            None,
        )
        self.assertIsNotNone(
            wait_product_cards,
            "_wait_product_cards 尚未实现",
        )

        actual = wait_product_cards()

        self.assertEqual(actual, product_cards)

    def test_wait_product_detail_returns_visible_detail_title(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        detail_title = FakeVisibleElement()
        collector = module.SeleniumCollector(
            driver=LocatorDriver(
                (
                    By.XPATH,
                    selectors.DETAIL_TITLE.query,
                ),
                detail_title,
            ),
            run_id="run-selenium-products",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_product_detail = getattr(
            collector,
            "_wait_product_detail",
            None,
        )
        self.assertIsNotNone(
            wait_product_detail,
            "_wait_product_detail 尚未实现",
        )

        actual = wait_product_detail()

        self.assertIs(actual, detail_title)

    def test_wait_comment_open_button_requires_clickable_state(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        open_button = FakeClickableElement()
        collector = module.SeleniumCollector(
            driver=LocatorDriver(
                (
                    By.CSS_SELECTOR,
                    selectors.COMMENT_OPEN_BUTTON.query,
                ),
                open_button,
            ),
            run_id="run-selenium-comments",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_comment_button = getattr(
            collector,
            "_wait_comment_open_button",
            None,
        )
        self.assertIsNotNone(
            wait_comment_button,
            "_wait_comment_open_button 尚未实现",
        )

        actual = wait_comment_button()

        self.assertIs(actual, open_button)

    def test_wait_comment_drawer_returns_visible_drawer(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        drawer = FakeVisibleElement()
        collector = module.SeleniumCollector(
            driver=LocatorDriver(
                (
                    By.CSS_SELECTOR,
                    selectors.COMMENT_DRAWER.query,
                ),
                drawer,
            ),
            run_id="run-selenium-comments",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_comment_drawer = getattr(
            collector,
            "_wait_comment_drawer",
            None,
        )
        self.assertIsNotNone(
            wait_comment_drawer,
            "_wait_comment_drawer 尚未实现",
        )

        actual = wait_comment_drawer()

        self.assertIs(actual, drawer)

    def test_wait_comment_items_returns_initial_comment_list(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        comment_items = [object(), object()]
        collector = module.SeleniumCollector(
            driver=LocatorListDriver(
                (
                    By.XPATH,
                    selectors.COMMENT_ITEMS.query,
                ),
                comment_items,
            ),
            run_id="run-selenium-comments",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_comment_items = getattr(
            collector,
            "_wait_comment_items",
            None,
        )
        self.assertIsNotNone(
            wait_comment_items,
            "_wait_comment_items 尚未实现",
        )

        actual = wait_comment_items()

        self.assertEqual(actual, comment_items)

    def test_wait_product_search_input_requires_clickable_state(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        search_input = FakeClickableElement()
        collector = module.SeleniumCollector(
            driver=LocatorDriver(
                (
                    By.CSS_SELECTOR,
                    selectors.SEARCH_INPUT.query,
                ),
                search_input,
            ),
            run_id="run-selenium-products",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_search_input = getattr(
            collector,
            "_wait_product_search_input",
            None,
        )
        self.assertIsNotNone(
            wait_search_input,
            "_wait_product_search_input 尚未实现",
        )

        actual = wait_search_input()

        self.assertIs(actual, search_input)

    def test_wait_next_page_button_requires_clickable_state(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        next_button = FakeClickableElement()
        collector = module.SeleniumCollector(
            driver=LocatorDriver(
                (
                    By.XPATH,
                    selectors.NEXT_PAGE_BUTTON.query,
                ),
                next_button,
            ),
            run_id="run-selenium-products",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_next_button = getattr(
            collector,
            "_wait_next_page_button",
            None,
        )
        self.assertIsNotNone(
            wait_next_button,
            "_wait_next_page_button 尚未实现",
        )

        actual = wait_next_button()

        self.assertIs(actual, next_button)

    def test_wait_live_navigation_requires_clickable_state(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        live_navigation = FakeClickableElement()
        collector = module.SeleniumCollector(
            driver=LocatorDriver(
                (
                    By.XPATH,
                    selectors.LIVE_NAV_TAB.query,
                ),
                live_navigation,
            ),
            run_id="run-selenium-live",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_live_navigation = getattr(
            collector,
            "_wait_live_navigation",
            None,
        )
        self.assertIsNotNone(
            wait_live_navigation,
            "_wait_live_navigation 尚未实现",
        )

        actual = wait_live_navigation()

        self.assertIs(actual, live_navigation)

    def test_wait_live_search_input_requires_clickable_state(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        search_input = FakeClickableElement()
        collector = module.SeleniumCollector(
            driver=LocatorDriver(
                (
                    By.XPATH,
                    selectors.LIVE_SEARCH_INPUT.query,
                ),
                search_input,
            ),
            run_id="run-selenium-live",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_live_search_input = getattr(
            collector,
            "_wait_live_search_input",
            None,
        )
        self.assertIsNotNone(
            wait_live_search_input,
            "_wait_live_search_input 尚未实现",
        )

        actual = wait_live_search_input()

        self.assertIs(actual, search_input)

    def test_wait_live_cards_returns_registered_live_items(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        live_cards = [object(), object()]
        collector = module.SeleniumCollector(
            driver=LocatorListDriver(
                (
                    By.XPATH,
                    selectors.LIVE_LIST_ITEMS.query,
                ),
                live_cards,
            ),
            run_id="run-selenium-live",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_live_cards = getattr(
            collector,
            "_wait_live_cards",
            None,
        )
        self.assertIsNotNone(
            wait_live_cards,
            "_wait_live_cards 尚未实现",
        )

        actual = wait_live_cards()

        self.assertEqual(actual, live_cards)

    def test_wait_live_detail_returns_visible_goods_title(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        goods_title = FakeVisibleElement()
        collector = module.SeleniumCollector(
            driver=LocatorDriver(
                (
                    By.XPATH,
                    selectors.LIVE_GOODS_TITLE.query,
                ),
                goods_title,
            ),
            run_id="run-selenium-live",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )
        wait_live_detail = getattr(
            collector,
            "_wait_live_detail",
            None,
        )
        self.assertIsNotNone(
            wait_live_detail,
            "_wait_live_detail 尚未实现",
        )

        actual = wait_live_detail()

        self.assertIs(actual, goods_title)


class SeleniumBehaviorTests(unittest.TestCase):
    def make_collector(
        self,
        driver,
        *,
        behavior_policy=None,
        live_behavior_policy=None,
        slept=None,
        random_source=None,
    ):
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        try:
            return module.SeleniumCollector(
                driver=driver,
                run_id="run-selenium-behavior",
                behavior_policy=behavior_policy,
                live_behavior_policy=live_behavior_policy,
                sleeper=(slept if slept is not None else []).append,
                random_source=random_source,
            )
        except TypeError as exc:
            self.fail(f"SeleniumCollector 尚未接收行为策略依赖：{exc}")

    def test_page_scroll_uses_action_chains_with_requested_distance(
        self,
    ) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        driver = BehaviorDriver()
        collector = module.SeleniumCollector(
            driver=driver,
            run_id="run-selenium-scroll",
        )

        self.assertTrue(
            hasattr(module, "ActionChains"),
            "selenium_collector 尚未导入 ActionChains",
        )
        scroll_page = getattr(collector, "_scroll_page", None)
        self.assertIsNotNone(scroll_page, "_scroll_page 尚未实现")

        with patch.object(module, "ActionChains", RecordingActionChains):
            scroll_page(480)

        self.assertEqual(
            driver.action_batches,
            [(('scroll', 0, 480),)],
        )

    def test_inner_container_scroll_targets_only_given_element(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        driver = BehaviorDriver()
        collector = module.SeleniumCollector(
            driver=driver,
            run_id="run-selenium-scroll",
        )
        drawer = BehaviorElement("comment-drawer")

        scroll_element = getattr(collector, "_scroll_element", None)
        self.assertIsNotNone(scroll_element, "_scroll_element 尚未实现")

        scroll_element(drawer, 900)

        self.assertEqual(drawer.scroll_top, 900)
        self.assertEqual(driver.script_scrolls, [(drawer, 900)])

    def test_disabled_product_behavior_has_no_interaction_or_pause(
        self,
    ) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        driver = BehaviorDriver()
        slept = []
        collector = self.make_collector(
            driver,
            behavior_policy=ReadOnlyBehaviorPolicy(enabled=False),
            slept=slept,
            random_source=PredictableRandom(),
        )

        with patch.object(module, "ActionChains", RecordingActionChains):
            collector._perform_read_only_behavior()

        self.assertEqual(driver.action_batches, [])
        self.assertEqual(slept, [])

    def test_product_behavior_limits_tab_views_scrolls_and_pauses(
        self,
    ) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        first_tab = BehaviorElement("parameters")
        second_tab = BehaviorElement("media")
        third_tab = BehaviorElement("recommendations")
        driver = BehaviorDriver(
            {
                selectors.PRODUCT_PARAMETERS_TAB.selenium_locator: [
                    first_tab
                ],
                selectors.PRODUCT_MEDIA_TAB.selenium_locator: [second_tab],
                selectors.STORE_RECOMMENDATION_TAB.selenium_locator: [
                    third_tab
                ],
            }
        )
        slept = []
        random_source = PredictableRandom(delay=0.4)
        collector = self.make_collector(
            driver,
            behavior_policy=ReadOnlyBehaviorPolicy(
                enabled=True,
                min_pause=0.3,
                max_pause=0.7,
                max_scrolls=2,
                max_tab_views=2,
            ),
            slept=slept,
            random_source=random_source,
        )

        with patch.object(module, "ActionChains", RecordingActionChains):
            collector._perform_read_only_behavior()

        self.assertEqual(
            driver.action_batches,
            [
                (("move", first_tab), ("click",)),
                (("move", second_tab), ("click",)),
                (("scroll", 0, 400),),
                (("scroll", 0, 400),),
            ],
        )
        self.assertEqual(random_source.sample_sizes, [2])
        self.assertEqual(
            random_source.uniform_bounds,
            [(0.3, 0.7)] * 4,
        )
        self.assertEqual(slept, [0.4] * 4)

    def test_live_behavior_has_bounded_scrolls_and_random_pauses(
        self,
    ) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        driver = BehaviorDriver()
        slept = []
        random_source = PredictableRandom(delay=2.5)
        collector = self.make_collector(
            driver,
            live_behavior_policy=LiveBehaviorPolicy(
                enabled=True,
                min_pause=2.0,
                max_pause=4.0,
                max_scrolls=3,
            ),
            slept=slept,
            random_source=random_source,
        )

        with patch.object(module, "ActionChains", RecordingActionChains):
            collector._perform_live_read_only_behavior()

        self.assertEqual(
            driver.action_batches,
            [(("scroll", 0, 400),)] * 3,
        )
        self.assertEqual(
            random_source.uniform_bounds,
            [(2.0, 4.0)] * 4,
        )
        self.assertEqual(slept, [2.5] * 4)


class SeleniumWindowTests(unittest.TestCase):
    def make_collector(self, driver):
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        return module.SeleniumCollector(
            driver=driver,
            run_id="run-selenium-window",
            wait_policy=WaitPolicy(
                timeout=0.05,
                poll_interval=0.001,
            ),
        )

    def temporary_new_tab(self, collector, opener):
        method = getattr(collector, "_temporary_new_tab", None)
        self.assertIsNotNone(
            method,
            "_temporary_new_tab 尚未实现",
        )
        return method(opener)

    def test_new_tab_scope_switches_closes_and_restores(self) -> None:
        driver = FakeWindowDriver()
        collector = self.make_collector(driver)

        with self.temporary_new_tab(
            collector,
            lambda: driver.open_window("detail"),
        ) as detail_handle:
            self.assertEqual(detail_handle, "detail")
            self.assertEqual(driver.current_window_handle, "detail")

        self.assertEqual(driver.window_handles, ["main"])
        self.assertEqual(driver.closed_handles, ["detail"])
        self.assertEqual(driver.current_window_handle, "main")

    def test_body_error_still_closes_new_tab_and_restores(self) -> None:
        driver = FakeWindowDriver()
        collector = self.make_collector(driver)

        with self.assertRaisesRegex(ValueError, "详情解析失败"):
            with self.temporary_new_tab(
                collector,
                lambda: driver.open_window("detail"),
            ):
                raise ValueError("详情解析失败")

        self.assertEqual(driver.window_handles, ["main"])
        self.assertEqual(driver.current_window_handle, "main")

    def test_opener_error_closes_partially_opened_tab(self) -> None:
        driver = FakeWindowDriver()
        collector = self.make_collector(driver)

        def failing_opener() -> None:
            driver.open_window("partial")
            raise RuntimeError("打开详情失败")

        with self.assertRaisesRegex(RuntimeError, "打开详情失败"):
            with self.temporary_new_tab(collector, failing_opener):
                pass

        self.assertEqual(driver.window_handles, ["main"])
        self.assertEqual(driver.current_window_handle, "main")

    def test_scope_preserves_old_tabs_and_closes_all_new_tabs(self) -> None:
        driver = FakeWindowDriver(handles=["main", "existing"])
        collector = self.make_collector(driver)

        def open_multiple() -> None:
            driver.open_window("detail")
            driver.open_window("popup")

        with self.temporary_new_tab(
            collector,
            open_multiple,
        ) as detail_handle:
            self.assertEqual(detail_handle, "detail")

        self.assertEqual(driver.window_handles, ["main", "existing"])
        self.assertEqual(driver.closed_handles, ["detail", "popup"])
        self.assertEqual(driver.current_window_handle, "main")

    def test_cleanup_error_does_not_mask_body_error(self) -> None:
        driver = FakeWindowDriver(close_error_handles={"detail"})
        collector = self.make_collector(driver)

        with self.assertRaisesRegex(ValueError, "采集失败"):
            with self.temporary_new_tab(
                collector,
                lambda: driver.open_window("detail"),
            ):
                raise ValueError("采集失败")

        self.assertEqual(driver.window_handles, ["main"])
        self.assertEqual(driver.current_window_handle, "main")

    def test_cleanup_error_is_raised_after_normal_body(self) -> None:
        driver = FakeWindowDriver(close_error_handles={"detail"})
        collector = self.make_collector(driver)

        with self.assertRaisesRegex(RuntimeError, "关闭窗口失败"):
            with self.temporary_new_tab(
                collector,
                lambda: driver.open_window("detail"),
            ):
                pass

        self.assertEqual(driver.window_handles, ["main"])
        self.assertEqual(driver.current_window_handle, "main")


if __name__ == "__main__":
    unittest.main()
