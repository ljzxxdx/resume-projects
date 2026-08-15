"""Selenium 商品、详情与评论采集流程测试。"""

from __future__ import annotations

import importlib
import unittest
from contextlib import contextmanager
from decimal import Decimal

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.keys import Keys

from taobao_collector import selectors
from taobao_collector.collectors.base import (
    CollectionResult,
    ProductFilterPolicy,
    ReadOnlyBehaviorPolicy,
    RetryPolicy,
    WaitPolicy,
)
from taobao_collector.models import (
    CommentRecord,
    Engine,
    ProductRecord,
)


class FakeElement:
    def __init__(
        self,
        *,
        text: str = "",
        attributes=None,
        children=None,
        child_lists=None,
    ) -> None:
        self.text = text
        self.attributes = dict(attributes or {})
        self.children = dict(children or {})
        self.child_lists = dict(child_lists or {})
        self.sent_keys = []
        self.clear_count = 0

    def get_attribute(self, name: str):
        return self.attributes.get(name)

    def find_element(self, by, query):
        locator = (by, query)
        if locator not in self.children:
            raise NoSuchElementException(f"未找到子元素：{locator!r}")
        return self.children[locator]

    def find_elements(self, by, query):
        value = self.child_lists.get((by, query), ())
        return list(value() if callable(value) else value)

    def send_keys(self, *values) -> None:
        self.sent_keys.extend(values)

    def clear(self) -> None:
        self.clear_count += 1


class AsyncCommentDrawer(FakeElement):
    def __init__(self, batches) -> None:
        self.batches = [list(batch) for batch in batches]
        self.batch_index = 0
        self.scroll_count = 0
        super().__init__(
            child_lists={
                selectors.COMMENT_ITEMS.selenium_locator: self.current_items,
            }
        )

    def current_items(self):
        return self.batches[self.batch_index]

    def advance(self) -> None:
        self.scroll_count += 1
        if self.batch_index < len(self.batches) - 1:
            self.batch_index += 1


class ScriptDriver:
    def execute_script(self, script, element, distance) -> None:
        if "scrollTop" not in script or distance <= 0:
            raise AssertionError("评论滚动必须是定向且为正距离")
        element.advance()


def make_comment(user_name: str, sku_info: str, content: str) -> FakeElement:
    return FakeElement(
        children={
            selectors.COMMENT_USER_NAME.selenium_locator: FakeElement(
                text=user_name
            ),
            selectors.COMMENT_SKU.selenium_locator: FakeElement(
                text=f"规格：{sku_info}"
            ),
            selectors.COMMENT_CONTENT.selenium_locator: FakeElement(
                text=content
            ),
        }
    )


class PaginationButton(FakeElement):
    def __init__(self, *, enabled: bool = True, attributes=None) -> None:
        super().__init__(attributes=attributes)
        self.enabled = enabled

    def is_enabled(self) -> bool:
        return self.enabled


class IndexablePaginationButton(PaginationButton):
    """兼容待修复列表逻辑，以隔离验证 is_enabled 分支。"""

    def __getitem__(self, index):
        if index != -1:
            raise IndexError(index)
        return self

    def attr(self, name: str):
        return self.get_attribute(name)


class ProductListDriver:
    def __init__(self, snapshots=None, buttons=None) -> None:
        self.snapshots = [list(items) for items in (snapshots or [])]
        self.buttons = list(buttons or [])
        self.snapshot_index = 0
        self.opened_urls = []

    def get(self, url: str) -> None:
        self.opened_urls.append(url)

    def find_elements(self, by, query):
        locator = (by, query)
        if locator == selectors.NEXT_PAGE_BUTTON.selenium_locator:
            return list(self.buttons)
        if locator != selectors.PRODUCT_LIST_ITEMS.selenium_locator:
            return []
        if not self.snapshots:
            return []
        index = min(self.snapshot_index, len(self.snapshots) - 1)
        self.snapshot_index += 1
        return list(self.snapshots[index])


class SeleniumProductNavigationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )

    def test_home_and_search_navigate_to_encoded_results_url(self) -> None:
        driver = ProductListDriver()
        collector = self.module.SeleniumCollector(
            driver=driver,
            run_id="run-selenium-search",
        )
        search_input = FakeElement()

        collector._open_home()
        collector._submit_product_search(search_input, "陶瓷 杯垫")

        self.assertEqual(
            driver.opened_urls,
            [
                self.module.HOME_URL,
                "https://s.taobao.com/search?q=%E9%99%B6%E7%93%B7+%E6%9D%AF%E5%9E%AB",
            ],
        )
        self.assertEqual(search_input.clear_count, 1)
        self.assertEqual(search_input.sent_keys, ["陶瓷 杯垫"])

    def test_page_signature_uses_web_element_href_attributes(self) -> None:
        collector = self.module.SeleniumCollector(
            driver=object(),
            run_id="run-selenium-signature",
        )
        cards = [
            FakeElement(attributes={"href": " https://item/1 "}),
            FakeElement(attributes={"href": "https://item/2"}),
        ]

        self.assertEqual(
            collector._product_page_signature(cards),
            ("https://item/1", "https://item/2"),
        )

    def test_next_page_handles_absent_disabled_and_clickable_buttons(self) -> None:
        cases = (
            ([], False, 0),
            ([PaginationButton(enabled=False)], False, 0),
            (
                [PaginationButton(attributes={"disabled": "disabled"})],
                False,
                0,
            ),
            (
                [PaginationButton(attributes={"aria-disabled": "true"})],
                False,
                0,
            ),
            ([PaginationButton()], True, 1),
        )
        for buttons, expected, click_count in cases:
            with self.subTest(expected=expected, buttons=buttons):
                driver = ProductListDriver(buttons=buttons)
                collector = self.module.SeleniumCollector(
                    driver=driver,
                    run_id="run-selenium-pagination",
                )
                clicked = []
                collector._move_and_click = clicked.append

                self.assertEqual(collector._open_next_product_page(), expected)
                self.assertEqual(len(clicked), click_count)

    def test_next_page_rejects_webdriver_disabled_button(self) -> None:
        button = IndexablePaginationButton(enabled=False)
        collector = self.module.SeleniumCollector(
            driver=ProductListDriver(buttons=[button]),
            run_id="run-selenium-disabled-pagination",
        )
        clicked = []
        collector._move_and_click = clicked.append

        self.assertFalse(collector._open_next_product_page())
        self.assertEqual(clicked, [])

    def test_page_change_wait_returns_true_on_change_and_false_on_timeout(
        self,
    ) -> None:
        old_card = FakeElement(attributes={"href": "https://item/old"})
        new_card = FakeElement(attributes={"href": "https://item/new"})
        changed_collector = self.module.SeleniumCollector(
            driver=ProductListDriver(snapshots=[[old_card], [new_card]]),
            run_id="run-selenium-page-change",
            wait_policy=WaitPolicy(timeout=0.05, poll_interval=0.001),
        )
        unchanged_collector = self.module.SeleniumCollector(
            driver=ProductListDriver(snapshots=[[old_card]]),
            run_id="run-selenium-page-timeout",
            wait_policy=WaitPolicy(timeout=0.01, poll_interval=0.001),
        )
        empty_collector = self.module.SeleniumCollector(
            driver=ProductListDriver(snapshots=[[]]),
            run_id="run-selenium-empty-page",
            wait_policy=WaitPolicy(timeout=0.01, poll_interval=0.001),
        )

        self.assertTrue(
            changed_collector._wait_product_page_change(
                ("https://item/old",)
            )
        )
        self.assertFalse(
            unchanged_collector._wait_product_page_change(
                ("https://item/old",)
            )
        )
        self.assertFalse(
            empty_collector._wait_product_page_change(
                ("https://item/old",)
            )
        )


class SeleniumProductParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )

    def product_data_class(self):
        data_class = getattr(self.module, "_ProductCardData", None)
        self.assertIsNotNone(data_class, "_ProductCardData 尚未实现")
        return data_class

    def test_product_card_parses_normalized_business_fields(self) -> None:
        collector = self.module.SeleniumCollector(
            driver=object(),
            run_id="run-selenium-product-parse",
        )
        parse_card = getattr(collector, "_parse_product_card", None)
        self.assertIsNotNone(parse_card, "_parse_product_card 尚未实现")
        card = FakeElement(
            attributes={
                "href": "https://item.taobao.com/item.htm?id=123456"
            },
            children={
                selectors.PRODUCT_TITLE.selenium_locator: FakeElement(
                    text="  手工陶瓷杯  "
                ),
                selectors.PRODUCT_PRICE.selenium_locator: FakeElement(
                    text="¥29.90"
                ),
                selectors.PRODUCT_SALES.selenium_locator: FakeElement(
                    text="1.2万+人付款"
                ),
            },
        )

        product = parse_card(card)

        self.assertEqual(product.product_id, "123456")
        self.assertEqual(product.source_url, card.get_attribute("href"))
        self.assertEqual(product.name, "手工陶瓷杯")
        self.assertEqual(product.price, Decimal("29.90"))
        self.assertEqual(product.sales_count, 12000)

    def test_records_use_selenium_engine_and_normalize_date_only_sku(
        self,
    ) -> None:
        collector = self.module.SeleniumCollector(
            driver=object(),
            run_id="run-selenium-records",
        )
        product = self.product_data_class()(
            product_id="123456",
            source_url="https://item.taobao.com/item.htm?id=123456",
            name="手工陶瓷杯",
            price=Decimal("29.90"),
            sales_count=12000,
        )
        parse_comment = getattr(collector, "_parse_comment_item", None)
        build_product = getattr(collector, "_build_product_record", None)
        self.assertIsNotNone(parse_comment, "_parse_comment_item 尚未实现")
        self.assertIsNotNone(build_product, "_build_product_record 尚未实现")
        item = make_comment("用户甲", "2026年8月2日", "做工细致")

        product_record = build_product(product, "陶瓷杯")
        comment_record = parse_comment(item, product, "陶瓷杯")

        self.assertIsInstance(product_record, ProductRecord)
        self.assertIsInstance(comment_record, CommentRecord)
        self.assertIs(product_record.engine, Engine.SELENIUM)
        self.assertIs(comment_record.engine, Engine.SELENIUM)
        self.assertEqual(comment_record.product_id, "123456")
        self.assertEqual(comment_record.sku_info, "无")
        self.assertEqual(comment_record.content, "做工细致")


class SeleniumCommentFlowTests(unittest.TestCase):
    def test_comments_scroll_until_limit_and_return_linked_records(self) -> None:
        module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )
        data_class = getattr(module, "_ProductCardData", None)
        self.assertIsNotNone(data_class, "_ProductCardData 尚未实现")
        collector = module.SeleniumCollector(
            driver=ScriptDriver(),
            run_id="run-selenium-comments",
            behavior_policy=ReadOnlyBehaviorPolicy(
                enabled=True,
                comment_min_pause=2.0,
                comment_max_pause=2.0,
            ),
            sleeper=(comment_pauses := []).append,
            wait_policy=WaitPolicy(
                timeout=0.02,
                poll_interval=0.001,
            ),
        )
        collect_comments = getattr(collector, "_collect_comments", None)
        self.assertIsNotNone(collect_comments, "_collect_comments 尚未实现")
        first = make_comment("用户甲", "白色", "做工细致")
        second = make_comment("用户乙", "蓝色", "包装完整")
        drawer = AsyncCommentDrawer(
            [[first], [first, second]]
        )
        product = data_class(
            product_id="comments-1",
            source_url="https://item.taobao.com/item.htm?id=comments-1",
            name="评论测试商品",
            price=Decimal("30"),
            sales_count=30,
        )

        records = collect_comments(
            drawer=drawer,
            product=product,
            keyword="陶瓷",
            comment_limit=2,
        )

        self.assertEqual(
            [record.content for record in records],
            ["做工细致", "包装完整"],
        )
        self.assertTrue(
            all(record.product_id == "comments-1" for record in records)
        )
        self.assertEqual(drawer.scroll_count, 1)
        self.assertEqual(comment_pauses, [2.0])


class SeleniumProductCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )

    def product_data_class(self):
        data_class = getattr(self.module, "_ProductCardData", None)
        self.assertIsNotNone(data_class, "_ProductCardData 尚未实现")
        return data_class

    def test_collection_validates_keyword_and_limits_before_navigation(
        self,
    ) -> None:
        collector = self.module.SeleniumCollector(
            driver=object(),
            run_id="run-selenium-validation",
        )
        collect_products = getattr(collector, "collect_products", None)
        self.assertIsNotNone(collect_products, "collect_products 尚未实现")

        invalid_arguments = (
            {"keyword": " ", "product_limit": 1, "comment_limit": 1},
            {"keyword": "陶瓷", "product_limit": 0, "comment_limit": 1},
            {"keyword": "陶瓷", "product_limit": 1, "comment_limit": 0},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    collect_products(**arguments)

    def test_collection_skips_low_sales_and_paginates_to_qualified_item(
        self,
    ) -> None:
        module = self.module
        data_class = self.product_data_class()
        low_sales = data_class(
            product_id="low",
            source_url="https://item.taobao.com/item.htm?id=low",
            name="低销量商品",
            price=Decimal("10"),
            sales_count=5,
        )
        qualified = data_class(
            product_id="qualified",
            source_url="https://item.taobao.com/item.htm?id=qualified",
            name="合格商品",
            price=Decimal("30"),
            sales_count=30,
        )

        class FlowCollector(module.SeleniumCollector):
            def __init__(self):
                super().__init__(
                    driver=object(),
                    run_id="run-selenium-flow",
                    filter_policy=ProductFilterPolicy(),
                )
                self.pages = [[low_sales], [qualified]]
                self.page_index = 0
                self.opened_details = []
                self.pacing_events = []
                self.search_terms = []
                self.block_steps = []

            def _open_home(self) -> None:
                pass

            def _check_block(self, step: str) -> None:
                self.block_steps.append(step)

            def _wait_product_search_input(self):
                return object()

            def _submit_product_search(self, search_input, keyword) -> None:
                self.search_terms.append(keyword)

            @contextmanager
            def _temporary_new_tab(self, opener):
                opener()
                yield "temporary"

            def _wait_product_cards(self):
                return self.pages[self.page_index]

            @staticmethod
            def _product_page_signature(cards):
                return tuple(card.product_id for card in cards)

            @staticmethod
            def _parse_product_card(card):
                return card

            def _collect_product_with_retries(
                self,
                *,
                card,
                product,
                keyword,
                comment_limit,
            ):
                self.opened_details.append(product.product_id)
                return (
                    ProductRecord(
                        run_id=self.run_id,
                        engine=Engine.SELENIUM,
                        keyword=keyword,
                        product_id=product.product_id,
                        source_url=product.source_url,
                        name=product.name,
                        price=product.price,
                        sales_count=product.sales_count,
                    ),
                    [
                        CommentRecord(
                            run_id=self.run_id,
                            engine=Engine.SELENIUM,
                            keyword=keyword,
                            product_id=product.product_id,
                            source_url=product.source_url,
                            user_name="用户甲",
                            sku_info="白色",
                            content="做工细致",
                        )
                    ],
                )

            def _open_next_product_page(self) -> bool:
                if self.page_index + 1 >= len(self.pages):
                    return False
                self.page_index += 1
                return True

            def _wait_product_page_change(self, previous_signature) -> bool:
                return True

            def _pace_product_transition(self) -> None:
                self.pacing_events.append("product")

            def _pace_page_transition(self) -> None:
                self.pacing_events.append("page")

        try:
            collector = FlowCollector()
        except TypeError as exc:
            self.fail(f"SeleniumCollector 尚未接收商品策略：{exc}")
        collect_products = getattr(collector, "collect_products", None)
        self.assertIsNotNone(collect_products, "collect_products 尚未实现")

        result = collect_products(
            keyword="  陶瓷  ",
            product_limit=1,
            comment_limit=1,
        )

        self.assertEqual(collector.search_terms, ["陶瓷"])
        self.assertEqual(
            collector.block_steps,
            ["home", "product_results", "product_results"],
        )
        self.assertEqual(collector.page_index, 1)
        self.assertEqual(collector.opened_details, ["qualified"])
        self.assertEqual(collector.pacing_events, ["page", "product"])
        self.assertEqual(
            [record.product_id for record in result.products],
            ["qualified"],
        )
        self.assertEqual(len(result.comments), 1)

    def test_collection_returns_empty_result_when_no_next_page_exists(
        self,
    ) -> None:
        module = self.module
        data_class = self.product_data_class()
        low_sales = data_class(
            product_id="low-only",
            source_url="https://item.taobao.com/item.htm?id=low-only",
            name="仅有的低销量商品",
            price=Decimal("10"),
            sales_count=5,
        )

        class NoNextPageCollector(module.SeleniumCollector):
            def __init__(self):
                super().__init__(driver=object(), run_id="run-no-next-page")

            def _open_home(self) -> None:
                pass

            def _wait_product_search_input(self):
                return object()

            def _submit_product_search(self, search_input, keyword) -> None:
                pass

            def _wait_product_cards(self):
                return [low_sales]

            @staticmethod
            def _product_page_signature(cards):
                return ("only-page",)

            @staticmethod
            def _parse_product_card(card):
                return card

            def _open_next_product_page(self) -> bool:
                return False

        result = NoNextPageCollector().collect_products(
            keyword="陶瓷",
            product_limit=1,
            comment_limit=1,
        )

        self.assertIsInstance(result, CollectionResult)
        self.assertEqual(result.products, ())
        self.assertEqual(result.comments, ())

    def test_comment_threshold_rejects_entire_product_before_comment_open(
        self,
    ) -> None:
        module = self.module
        data_class = self.product_data_class()
        product = data_class(
            product_id="few-comments",
            source_url="https://item.taobao.com/item.htm?id=few-comments",
            name="评论不足商品",
            price=Decimal("30"),
            sales_count=30,
        )

        class DetailCollector(module.SeleniumCollector):
            def __init__(self):
                super().__init__(
                    driver=object(),
                    run_id="run-selenium-comment-filter",
                    filter_policy=ProductFilterPolicy(),
                )
                self.comment_opened = False

            @contextmanager
            def _temporary_new_tab(self, opener):
                opener()
                yield "detail"

            def _move_and_click(self, element) -> None:
                pass

            def _wait_product_detail(self):
                return object()

            def _parse_total_comments(self):
                return 19

            def _wait_comment_open_button(self):
                self.comment_opened = True
                return object()

        try:
            collector = DetailCollector()
        except TypeError as exc:
            self.fail(f"SeleniumCollector 尚未接收商品策略：{exc}")
        collect_one = getattr(
            collector,
            "_collect_product_with_retries",
            None,
        )
        self.assertIsNotNone(
            collect_one,
            "_collect_product_with_retries 尚未实现",
        )

        collected = collect_one(
            card=object(),
            product=product,
            keyword="陶瓷",
            comment_limit=10,
        )

        self.assertIsNone(collected)
        self.assertFalse(collector.comment_opened)

    def test_detail_failure_reopens_tab_with_finite_retry_delay(self) -> None:
        module = self.module
        data_class = self.product_data_class()
        product = data_class(
            product_id="retry-product",
            source_url="https://item.taobao.com/item.htm?id=retry-product",
            name="重试商品",
            price=Decimal("30"),
            sales_count=30,
        )
        retry_delays = []

        class RetryCollector(module.SeleniumCollector):
            def __init__(self):
                super().__init__(
                    driver=object(),
                    run_id="run-selenium-retry",
                    retry_policy=RetryPolicy(
                        max_retries=1,
                        base_delay=1.0,
                    ),
                    sleeper=retry_delays.append,
                )
                self.detail_attempts = 0
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
                self.detail_attempts += 1
                if self.detail_attempts == 1:
                    raise RuntimeError("首次详情加载失败")
                return object()

            def _perform_read_only_behavior(self) -> None:
                pass

            def _parse_total_comments(self):
                return 20

            def _wait_comment_open_button(self):
                return object()

            def _wait_comment_drawer(self):
                return object()

            def _collect_comments(self, **kwargs):
                return []

        collector = RetryCollector()
        collect_one = getattr(
            collector,
            "_collect_product_with_retries",
            None,
        )
        self.assertIsNotNone(
            collect_one,
            "_collect_product_with_retries 尚未实现",
        )

        collected = collect_one(
            card=object(),
            product=product,
            keyword="陶瓷",
            comment_limit=1,
        )

        self.assertIsNotNone(collected)
        self.assertEqual(collected[0].product_id, "retry-product")
        self.assertEqual(retry_delays, [1.0])
        self.assertEqual(collector.detail_attempts, 2)
        self.assertEqual(collector.closed_scopes, 2)

    def test_success_uses_waited_comment_drawer_as_collection_root(
        self,
    ) -> None:
        module = self.module
        data_class = self.product_data_class()
        product = data_class(
            product_id="drawer-root",
            source_url="https://item.taobao.com/item.htm?id=drawer-root",
            name="评论抽屉商品",
            price=Decimal("30"),
            sales_count=30,
        )
        expected_drawer = object()

        class DrawerCollector(module.SeleniumCollector):
            def __init__(self):
                super().__init__(
                    driver=object(),
                    run_id="run-selenium-drawer-root",
                    behavior_policy=ReadOnlyBehaviorPolicy(
                        enabled=True,
                        max_scrolls=0,
                        max_tab_views=0,
                    ),
                )
                self.collection_root = None
                self.pace_count = 0

            @contextmanager
            def _temporary_new_tab(self, opener):
                opener()
                yield "detail"

            def _move_and_click(self, element) -> None:
                pass

            def _wait_product_detail(self):
                return object()

            def _perform_read_only_behavior(self) -> None:
                pass

            def _pace(self) -> None:
                self.pace_count += 1

            def _parse_total_comments(self):
                return 20

            def _wait_comment_open_button(self):
                return object()

            def _wait_comment_drawer(self):
                return expected_drawer

            def _collect_comments(self, *, drawer, **kwargs):
                self.collection_root = drawer
                return []

        collector = DrawerCollector()

        collected = collector._collect_product_with_retries(
            card=object(),
            product=product,
            keyword="陶瓷",
            comment_limit=1,
        )

        self.assertIsNotNone(collected)
        self.assertIs(collector.collection_root, expected_drawer)
        self.assertEqual(collector.pace_count, 2)


if __name__ == "__main__":
    unittest.main()
