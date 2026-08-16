"""Behavior tests for the migrated DrissionPage collection workflow."""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

from taobao_collector import parsers, selectors
from taobao_collector.collectors import base, drission_collector


class FakeWait:
    def __init__(self) -> None:
        self.loaded: List[str] = []
        self.timeouts: List[Optional[float]] = []
        self.succeeds = True

    def eles_loaded(
        self,
        locator: str,
        *,
        timeout: Optional[float] = None,
        raise_err: Optional[bool] = None,
    ) -> bool:
        self.loaded.append(locator)
        self.timeouts.append(timeout)
        if not self.succeeds and raise_err:
            raise TimeoutError(f"等待元素超时：{locator}")
        return self.succeeds


class FakeClick:
    def __init__(
        self,
        target=None,
        on_click: Optional[Callable[[], None]] = None,
    ) -> None:
        self.target = target
        self.on_click = on_click
        self.count = 0

    def __call__(self) -> None:
        self.count += 1
        if self.on_click is not None:
            self.on_click()

    def for_new_tab(self):
        self.count += 1
        if isinstance(self.target, list):
            return self.target.pop(0)
        if isinstance(self.target, BaseException):
            raise self.target
        return self.target


class FakeScroll:
    def __init__(
        self,
        on_down: Optional[Callable[[], None]] = None,
    ) -> None:
        self.on_down = on_down
        self.to_see_count = 0
        self.down_count = 0

    def to_see(self) -> None:
        self.to_see_count += 1

    def down(self, pixel: int = 300) -> None:
        self.down_count += 1
        if self.on_down is not None:
            self.on_down()


class FakeElement:
    def __init__(
        self,
        *,
        text: str = "",
        href: str = "",
        children: Optional[Dict[str, "FakeElement"]] = None,
        child_lists: Optional[Dict[str, object]] = None,
        target_tab=None,
        on_click: Optional[Callable[[], None]] = None,
        on_scroll: Optional[Callable[[], None]] = None,
    ) -> None:
        self.text = text
        self.href = href
        self.children = children or {}
        self.child_lists = child_lists or {}
        self.click = FakeClick(target_tab, on_click)
        self.scroll = FakeScroll(on_scroll)
        self.input_values: List[str] = []

    def attr(self, name: str):
        return self.href if name == "href" else None

    def ele(self, locator: str) -> "FakeElement":
        return self.children[locator]

    def eles(self, locator: str) -> List["FakeElement"]:
        value = self.child_lists[locator]
        return list(value() if callable(value) else value)

    def input(self, value: str) -> None:
        self.input_values.append(value)


class FakeActions:
    def __init__(self, on_scroll: Optional[Callable[[], None]] = None) -> None:
        self.on_scroll = on_scroll
        self.scroll_count = 0
        self.move_targets: List[FakeElement] = []

    def scroll(self, *, delta_y: int) -> None:
        self.scroll_count += 1
        if self.on_scroll is not None:
            self.on_scroll()

    def move_to(self, *, ele_or_loc: FakeElement) -> "FakeActions":
        self.move_targets.append(ele_or_loc)
        return self


class FakeTab:
    def __init__(
        self,
        *,
        elements: Optional[Dict[str, FakeElement]] = None,
        element_lists: Optional[Dict[str, Iterable[FakeElement]]] = None,
        url: str = "",
        on_scroll: Optional[Callable[[], None]] = None,
    ) -> None:
        self.elements = elements or {}
        self.element_lists = element_lists or {}
        self.url = url
        self.wait = FakeWait()
        self.actions = FakeActions(on_scroll)
        self.scroll = FakeScroll()
        self.timeout = 0.02
        self.closed = False
        self.visited_urls: List[str] = []

    def get(self, url: str) -> None:
        self.visited_urls.append(url)
        self.url = url

    def ele(
        self,
        locator: str,
        *,
        timeout: Optional[float] = None,
    ):
        if timeout == 0:
            return self.elements.get(locator)
        return self.elements[locator]

    def eles(self, locator: str) -> List[FakeElement]:
        value = self.element_lists.get(locator, [])
        return list(value() if callable(value) else value)

    def close(self) -> None:
        self.closed = True


class FakeBrowserWait:
    def __init__(self, tab_ids: Iterable[str]) -> None:
        self.tab_ids = list(tab_ids)
        self.timeouts: List[Optional[float]] = []

    def new_tab(
        self,
        *,
        curr_tab: FakeTab,
        raise_err: bool,
        timeout: Optional[float] = None,
    ) -> str:
        self.timeouts.append(timeout)
        if not self.tab_ids:
            raise AssertionError("没有配置待打开的新标签页")
        return self.tab_ids.pop(0)


class FakeBrowser:
    def __init__(
        self,
        *,
        home_tab: FakeTab,
        tabs: Dict[str, FakeTab],
        tab_ids: Iterable[str],
    ) -> None:
        self.latest_tab = home_tab
        self.tabs = tabs
        self.wait = FakeBrowserWait(tab_ids)

    def get_tab(self, tab_id: str) -> FakeTab:
        return self.tabs[tab_id]

    @property
    def tab_ids(self) -> List[str]:
        return [
            tab_id
            for tab_id, tab in self.tabs.items()
            if not tab.closed
        ]


class PagedResultsTab(FakeTab):
    def __init__(self, pages: List[List[FakeElement]]) -> None:
        self.pages = pages
        self.page_index = 0
        self.next_button = FakeElement(on_click=self.advance)
        super().__init__(
            element_lists={
                selectors.PRODUCT_LIST_ITEMS.drission_locator:
                    self.current_cards,
                selectors.NEXT_PAGE_BUTTON.drission_locator:
                    self.current_next_buttons,
            },
            url="https://s.taobao.com/search",
        )

    def current_cards(self) -> List[FakeElement]:
        return self.pages[self.page_index]

    def current_next_buttons(self) -> List[FakeElement]:
        if self.page_index < len(self.pages) - 1:
            return [self.next_button]
        return []

    def advance(self) -> None:
        self.page_index += 1


class StuckResultsTab(FakeTab):
    def __init__(self, cards: List[FakeElement]) -> None:
        self.next_attempts = 0
        self.next_button = FakeElement(on_click=self.record_next_attempt)
        super().__init__(
            element_lists={
                selectors.PRODUCT_LIST_ITEMS.drission_locator: cards,
                selectors.NEXT_PAGE_BUTTON.drission_locator: [
                    self.next_button
                ],
            },
            url="https://s.taobao.com/search",
        )

    def record_next_attempt(self) -> None:
        self.next_attempts += 1
        if self.next_attempts > 1:
            raise AssertionError("同一结果页不应重复点击下一页")


class AsyncPagedResultsTab(FakeTab):
    def __init__(
        self,
        first_page: List[FakeElement],
        second_page: List[FakeElement],
    ) -> None:
        self.first_page = first_page
        self.second_page = second_page
        self.next_clicked = False
        self.polls_after_click = 0
        self.next_button = FakeElement(on_click=self.start_page_change)
        super().__init__(
            element_lists={
                selectors.PRODUCT_LIST_ITEMS.drission_locator:
                    self.current_cards,
                selectors.NEXT_PAGE_BUTTON.drission_locator:
                    self.current_next_buttons,
            },
            url="https://s.taobao.com/search",
        )

    def start_page_change(self) -> None:
        self.next_clicked = True

    def current_cards(self) -> List[FakeElement]:
        if not self.next_clicked:
            return self.first_page
        self.polls_after_click += 1
        if self.polls_after_click == 1:
            return self.first_page
        return self.second_page

    def current_next_buttons(self) -> List[FakeElement]:
        return [] if self.polls_after_click >= 2 else [self.next_button]


class CommentDrawer(FakeElement):
    def __init__(self, batches: List[List[FakeElement]]) -> None:
        self.batches = batches
        self.batch_index = 0
        super().__init__(
            child_lists={
                selectors.COMMENT_ITEMS.drission_locator:
                    self.current_batch,
            },
            on_scroll=self.advance,
        )
        self.timeout = 0.02

    def current_batch(self) -> List[FakeElement]:
        return self.batches[self.batch_index]

    def advance(self) -> None:
        if self.batch_index < len(self.batches) - 1:
            self.batch_index += 1


class AsyncCommentDrawer(CommentDrawer):
    def __init__(self, batches: List[List[FakeElement]]) -> None:
        self.scroll_started = False
        self.reads_after_scroll = 0
        super().__init__(batches)
        self.scroll = FakeScroll(self.start_scroll)

    def start_scroll(self) -> None:
        self.scroll_started = True

    def current_batch(self) -> List[FakeElement]:
        if not self.scroll_started:
            return self.batches[0]
        self.reads_after_scroll += 1
        if self.reads_after_scroll == 1:
            return self.batches[0]
        return self.batches[-1]


def make_comment(
    user_name: str,
    sku_info: str,
    content: str,
) -> FakeElement:
    return FakeElement(
        children={
            selectors.COMMENT_USER_NAME.drission_locator:
                FakeElement(text=user_name),
            selectors.COMMENT_SKU.drission_locator:
                FakeElement(text=f"规格：{sku_info}"),
            selectors.COMMENT_CONTENT.drission_locator:
                FakeElement(text=content),
        }
    )


def make_detail(
    comment_total: int,
    batches: Optional[List[List[FakeElement]]] = None,
    *,
    async_growth: bool = False,
) -> FakeTab:
    drawer_class = AsyncCommentDrawer if async_growth else CommentDrawer
    drawer = drawer_class(batches or [[]])
    return FakeTab(
        elements={
            selectors.DETAIL_TITLE.drission_locator:
                FakeElement(text=f"用户评价：{comment_total}"),
            selectors.COMMENT_OPEN_BUTTON.drission_locator:
                FakeElement(),
            selectors.COMMENT_DRAWER.drission_locator: drawer,
        },
        url="https://item.taobao.com/item.htm",
        on_scroll=(
            drawer.start_scroll
            if isinstance(drawer, AsyncCommentDrawer)
            else drawer.advance
        ),
    )


def make_product_card(
    *,
    product_id: str,
    name: str,
    price: str,
    sales: str,
    detail_tab=None,
) -> FakeElement:
    return FakeElement(
        href=f"https://item.taobao.com/item.htm?id={product_id}",
        target_tab=detail_tab,
        children={
            selectors.PRODUCT_TITLE.drission_locator:
                FakeElement(text=name),
            selectors.PRODUCT_PRICE.drission_locator:
                FakeElement(text=price),
            selectors.PRODUCT_SALES.drission_locator:
                FakeElement(text=sales),
        },
    )


def make_product_browser(cards: List[FakeElement]):
    search_input = FakeElement()
    home = FakeTab(
        elements={
            selectors.SEARCH_INPUT.drission_locator: search_input,
        }
    )
    results = FakeTab(
        element_lists={
            selectors.PRODUCT_LIST_ITEMS.drission_locator: cards,
        },
        url="https://s.taobao.com/search",
    )
    browser = FakeBrowser(
        home_tab=home,
        tabs={"products": results},
        tab_ids=["products"],
    )
    return browser, search_input, results


def make_paged_product_browser(pages: List[List[FakeElement]]):
    search_input = FakeElement()
    home = FakeTab(
        elements={
            selectors.SEARCH_INPUT.drission_locator: search_input,
        }
    )
    results = PagedResultsTab(pages)
    browser = FakeBrowser(
        home_tab=home,
        tabs={"products": results},
        tab_ids=["products"],
    )
    return browser, results


def make_stuck_product_browser(cards: List[FakeElement]):
    search_input = FakeElement()
    home = FakeTab(
        elements={
            selectors.SEARCH_INPUT.drission_locator: search_input,
        }
    )
    results = StuckResultsTab(cards)
    browser = FakeBrowser(
        home_tab=home,
        tabs={"products": results},
        tab_ids=["products"],
    )
    return browser, results


def make_async_product_browser(
    first_page: List[FakeElement],
    second_page: List[FakeElement],
):
    search_input = FakeElement()
    home = FakeTab(
        elements={
            selectors.SEARCH_INPUT.drission_locator: search_input,
        }
    )
    results = AsyncPagedResultsTab(first_page, second_page)
    browser = FakeBrowser(
        home_tab=home,
        tabs={"products": results},
        tab_ids=["products"],
    )
    return browser, results


def make_live_browser(
    *,
    goods_title: str,
    link_href: str = "https://live.taobao.com/room?id=live-100",
):
    detail = FakeTab(
        elements={
            selectors.LIVE_GOODS_TITLE.drission_locator:
                FakeElement(text=goods_title),
        },
        url="https://live.taobao.com/room?id=live-100",
    )
    link = FakeElement(
        href=link_href,
        target_tab=detail,
    )
    card = FakeElement(
        children={
            selectors.LIVE_LINK_BUTTON.drission_locator: link,
            selectors.LIVE_ACCOUNT_NAME.drission_locator:
                FakeElement(text="德化陶瓷直播"),
            selectors.LIVE_INTRODUCTION.drission_locator:
                FakeElement(text="手工陶瓷专场"),
        },
        child_lists={
            selectors.LIVE_INFO_COUNTS.drission_locator: [
                FakeElement(text="3.2万观看"),
                FakeElement(text="8.6万粉丝"),
            ],
        },
    )
    nav = FakeElement()
    home = FakeTab(
        elements={
            selectors.LIVE_NAV_TAB.drission_locator: nav,
        }
    )
    search_input = FakeElement()
    results = FakeTab(
        elements={
            selectors.LIVE_SEARCH_INPUT.drission_locator: search_input,
        },
        element_lists={
            selectors.LIVE_LIST_ITEMS.drission_locator: [card],
        },
        url="https://live.taobao.com/search",
    )
    browser = FakeBrowser(
        home_tab=home,
        tabs={"live": results},
        tab_ids=["live"],
    )
    return browser, search_input, results, detail


class DrissionCollectorTests(unittest.TestCase):
    def collector_class(self):
        collector_class = getattr(
            drission_collector,
            "DrissionCollector",
            None,
        )
        self.assertIsNotNone(collector_class, "DrissionCollector 尚未实现")
        return collector_class

    def test_products_skip_low_sales_and_collect_qualified_records(self) -> None:
        first_comment = make_comment("用户甲", "白色", "做工细致")
        second_comment = make_comment("用户乙", "蓝色", "包装完整")
        accepted_detail = make_detail(
            25,
            [[first_comment], [first_comment, second_comment]],
        )
        cards = [
            make_product_card(
                product_id="1001",
                name="低销量商品",
                price="19.9",
                sales="10人付款",
            ),
            make_product_card(
                product_id="1002",
                name="青花瓷杯",
                price="¥39.90",
                sales="已售1.2万+",
                detail_tab=accepted_detail,
            ),
        ]
        browser, search_input, results_tab = make_product_browser(cards)
        collector = self.collector_class()(
            browser,
            run_id="run-products",
        )
        pacing_events: List[str] = []
        collector._pace_product_transition = (
            lambda: pacing_events.append("product")
        )

        result = collector.collect_products(
            keyword="德化瓷",
            product_limit=1,
            comment_limit=2,
        )

        self.assertEqual(search_input.input_values, ["德化瓷\n"])
        self.assertEqual(len(result.products), 1)
        self.assertEqual(result.products[0].product_id, "1002")
        self.assertEqual(result.products[0].price, Decimal("39.90"))
        self.assertEqual(result.products[0].sales_count, 12000)
        self.assertEqual(
            [comment.content for comment in result.comments],
            ["做工细致", "包装完整"],
        )
        self.assertTrue(accepted_detail.closed)
        self.assertTrue(results_tab.closed)
        self.assertEqual(pacing_events, ["product"])

    def test_disabled_filter_keeps_low_sales_and_comment_product(self) -> None:
        detail = make_detail(
            2,
            [[make_comment("用户甲", "小号", "符合描述")]],
        )
        card = make_product_card(
            product_id="2001",
            name="小众商品",
            price="12",
            sales="5人付款",
            detail_tab=detail,
        )
        browser, _, _ = make_product_browser([card])
        collector = self.collector_class()(
            browser,
            run_id="run-unfiltered",
            filter_policy=base.ProductFilterPolicy(enabled=False),
        )

        result = collector.collect_products(
            keyword="手工饰品",
            product_limit=1,
            comment_limit=1,
        )

        self.assertEqual(len(result.products), 1)
        self.assertEqual(len(result.comments), 1)
        self.assertTrue(detail.closed)

    def test_products_continue_to_next_page_until_limit_is_met(self) -> None:
        accepted_detail = make_detail(
            20,
            [[make_comment("用户乙", "标准款", "质量很好")]],
        )
        first_page = [
            make_product_card(
                product_id="page-1-low",
                name="第一页低销量商品",
                price="10",
                sales="5人付款",
            )
        ]
        second_page = [
            make_product_card(
                product_id="page-2-ok",
                name="第二页合格商品",
                price="30",
                sales="30人付款",
                detail_tab=accepted_detail,
            )
        ]
        browser, results_tab = make_paged_product_browser(
            [first_page, second_page]
        )
        collector = self.collector_class()(
            browser,
            run_id="run-pagination",
        )
        pacing_events: List[str] = []
        collector._pace_product_transition = (
            lambda: pacing_events.append("product")
        )
        collector._pace_page_transition = (
            lambda: pacing_events.append("page")
        )

        result = collector.collect_products(
            keyword="陶瓷",
            product_limit=1,
            comment_limit=1,
        )

        self.assertEqual(
            [product.product_id for product in result.products],
            ["page-2-ok"],
        )
        self.assertEqual(results_tab.next_button.click.count, 1)
        self.assertTrue(accepted_detail.closed)
        self.assertTrue(results_tab.closed)
        self.assertEqual(pacing_events, ["page", "product"])

    def test_products_stop_when_next_page_does_not_change_results(self) -> None:
        low_sales_card = make_product_card(
            product_id="stuck-low",
            name="低销量商品",
            price="10",
            sales="5人付款",
        )
        browser, results_tab = make_stuck_product_browser(
            [low_sales_card]
        )
        collector = self.collector_class()(
            browser,
            run_id="run-stuck-page",
        )

        try:
            result = collector.collect_products(
                keyword="陶瓷",
                product_limit=1,
                comment_limit=1,
            )
        except AssertionError as exc:
            self.fail(f"结果页未变化时发生了重复翻页：{exc}")

        self.assertEqual(result.products, ())
        self.assertEqual(results_tab.next_attempts, 1)
        self.assertTrue(results_tab.closed)

    def test_products_wait_for_async_next_page_result_change(self) -> None:
        accepted_detail = make_detail(
            20,
            [[make_comment("用户乙", "标准款", "质量很好")]],
        )
        first_page = [
            make_product_card(
                product_id="async-low",
                name="第一页低销量商品",
                price="10",
                sales="5人付款",
            )
        ]
        second_page = [
            make_product_card(
                product_id="async-ok",
                name="异步加载后的合格商品",
                price="30",
                sales="30人付款",
                detail_tab=accepted_detail,
            )
        ]
        browser, results_tab = make_async_product_browser(
            first_page,
            second_page,
        )
        collector = self.collector_class()(
            browser,
            run_id="run-async-page",
        )

        result = collector.collect_products(
            keyword="陶瓷",
            product_limit=1,
            comment_limit=1,
        )

        self.assertEqual(
            [product.product_id for product in result.products],
            ["async-ok"],
        )
        self.assertGreaterEqual(results_tab.polls_after_click, 2)

    def test_comments_scroll_drawer_and_wait_for_async_growth(self) -> None:
        first_comment = make_comment("用户甲", "白色", "做工细致")
        second_comment = make_comment("用户乙", "蓝色", "包装完整")
        detail = make_detail(
            20,
            [[first_comment], [first_comment, second_comment]],
            async_growth=True,
        )
        card = make_product_card(
            product_id="async-comments",
            name="异步评论商品",
            price="30",
            sales="30人付款",
            detail_tab=detail,
        )
        browser, _, _ = make_product_browser([card])
        collector = self.collector_class()(
            browser,
            run_id="run-async-comments",
        )
        comment_pauses: List[str] = []
        collector._pace_comment_load = (
            lambda: comment_pauses.append("comment")
        )

        result = collector.collect_products(
            keyword="陶瓷",
            product_limit=1,
            comment_limit=2,
        )

        self.assertEqual(
            [comment.content for comment in result.comments],
            ["做工细致", "包装完整"],
        )
        drawer = detail.ele(
            selectors.COMMENT_DRAWER.drission_locator
        )
        self.assertEqual(drawer.scroll.down_count, 1)
        self.assertGreaterEqual(drawer.reads_after_scroll, 2)
        self.assertEqual(comment_pauses, ["comment"])

    def test_product_list_wait_timeout_is_not_reported_as_empty(self) -> None:
        browser, _, results_tab = make_product_browser([])
        results_tab.wait.succeeds = False
        collector = self.collector_class()(
            browser,
            run_id="run-list-timeout",
        )

        with self.assertRaises(TimeoutError):
            collector.collect_products(
                keyword="陶瓷",
                product_limit=1,
                comment_limit=1,
            )

        self.assertTrue(results_tab.closed)

    def test_read_only_behavior_is_bounded_and_uses_safe_tabs(self) -> None:
        detail = make_detail(
            20,
            [[make_comment("用户甲", "白色", "做工细致")]],
        )
        safe_parameter_tab = FakeElement()
        safe_media_tab = FakeElement()
        detail.elements.update(
            {
                selectors.PRODUCT_PARAMETERS_TAB.drission_locator:
                    safe_parameter_tab,
                selectors.PRODUCT_MEDIA_TAB.drission_locator:
                    safe_media_tab,
            }
        )
        card = make_product_card(
            product_id="readonly",
            name="只读行为商品",
            price="30",
            sales="30人付款",
            detail_tab=detail,
        )
        browser, _, _ = make_product_browser([card])
        pauses: List[float] = []
        try:
            collector = self.collector_class()(
                browser,
                run_id="run-readonly",
                behavior_policy=base.ReadOnlyBehaviorPolicy(
                    enabled=True,
                    min_pause=0.1,
                    max_pause=0.2,
                    max_scrolls=2,
                    max_tab_views=1,
                ),
                sleeper=pauses.append,
            )
        except TypeError as exc:
            self.fail(f"采集器尚未支持只读行为策略：{exc}")

        result = collector.collect_products(
            keyword="陶瓷",
            product_limit=1,
            comment_limit=1,
        )

        self.assertEqual(len(result.products), 1)
        clicked_tabs = (
            safe_parameter_tab.click.count
            + safe_media_tab.click.count
        )
        self.assertEqual(clicked_tabs, 1)
        self.assertEqual(len(detail.actions.move_targets), 1)
        self.assertEqual(detail.scroll.down_count, 2)
        self.assertEqual(len(pauses), 6)
        for pause in pauses[:5]:
            self.assertGreaterEqual(pause, 0.1)
            self.assertLessEqual(pause, 0.2)
        self.assertGreaterEqual(pauses[-1], 5.0)
        self.assertLessEqual(pauses[-1], 8.0)

    def test_product_retry_reopens_and_closes_each_detail_tab(self) -> None:
        invalid_comment = FakeElement(
            children={
                selectors.COMMENT_USER_NAME.drission_locator:
                    FakeElement(text="用户甲"),
                selectors.COMMENT_SKU.drission_locator:
                    FakeElement(text="规格：白色"),
            }
        )
        failed_detail = make_detail(20, [[invalid_comment]])
        successful_detail = make_detail(
            20,
            [[make_comment("用户乙", "蓝色", "包装完整")]],
        )
        card = make_product_card(
            product_id="retry-ok",
            name="重试商品",
            price="30",
            sales="30人付款",
            detail_tab=[failed_detail, successful_detail],
        )
        browser, _, _ = make_product_browser([card])
        retry_pauses: List[float] = []
        try:
            collector = self.collector_class()(
                browser,
                run_id="run-retry",
                retry_policy=base.RetryPolicy(
                    max_retries=1,
                    base_delay=1.0,
                ),
                sleeper=retry_pauses.append,
            )
        except TypeError as exc:
            self.fail(f"采集器尚未支持有限重试策略：{exc}")

        result = collector.collect_products(
            keyword="陶瓷",
            product_limit=1,
            comment_limit=1,
        )

        self.assertEqual(
            [product.product_id for product in result.products],
            ["retry-ok"],
        )
        self.assertEqual(retry_pauses, [1.0])
        self.assertEqual(card.click.count, 2)
        self.assertTrue(failed_detail.closed)
        self.assertTrue(successful_detail.closed)

    def test_failed_detail_open_checks_and_closes_new_orphan_tab(
        self,
    ) -> None:
        card = make_product_card(
            product_id="orphan",
            name="异常开页商品",
            price="30",
            sales="30人付款",
        )
        browser, _, results_tab = make_product_browser([card])
        orphan_tab = FakeTab(url="https://login.taobao.com/")

        class OrphaningClick:
            count = 0

            def for_new_tab(self):
                self.count += 1
                browser.tabs["orphan"] = orphan_tab
                raise TimeoutError("等待详情标签超时")

        class BlockingGuard:
            def inspect(self, tab, *, run_id, engine, step) -> None:
                if tab is orphan_tab:
                    raise base.CollectionBlockedError(
                        step=step,
                        reason="进入淘宝登录页面",
                        screenshot_path=None,
                    )

        card.click = OrphaningClick()
        collector = self.collector_class()(
            browser,
            run_id="run-orphan",
            block_guard=BlockingGuard(),
        )

        with self.assertRaises(base.CollectionBlockedError):
            collector.collect_products(
                keyword="陶瓷",
                product_limit=1,
                comment_limit=1,
            )

        self.assertTrue(orphan_tab.closed)
        self.assertTrue(results_tab.closed)
        self.assertEqual(card.click.count, 1)

    def test_failed_results_open_rechecks_main_tab_for_blocking(
        self,
    ) -> None:
        card = make_product_card(
            product_id="blocked-results",
            name="搜索阻塞商品",
            price="30",
            sales="30人付款",
        )
        browser, _, _ = make_product_browser([card])
        home_tab = browser.latest_tab

        class FailingWait:
            def new_tab(self, *, curr_tab, raise_err, timeout=None):
                home_tab.url = "https://login.taobao.com/"
                raise TimeoutError("等待结果标签超时")

        class BlockingGuard:
            def inspect(self, tab, *, run_id, engine, step) -> None:
                if "login.taobao.com" in tab.url:
                    raise base.CollectionBlockedError(
                        step=step,
                        reason="进入淘宝登录页面",
                        screenshot_path=None,
                    )

        browser.wait = FailingWait()
        collector = self.collector_class()(
            browser,
            run_id="run-results-blocked",
            block_guard=BlockingGuard(),
        )

        with self.assertRaises(base.CollectionBlockedError):
            collector.collect_products(
                keyword="陶瓷",
                product_limit=1,
                comment_limit=1,
            )

    def test_product_search_supports_same_tab_navigation_without_closing_main(
        self,
    ) -> None:
        detail = make_detail(
            20,
            [[make_comment("样例用户", "青釉", "样例评论")]],
        )
        card = make_product_card(
            product_id="same-tab",
            name="同页结果商品",
            price="39.9",
            sales="已售100",
            detail_tab=detail,
        )
        search_input = FakeElement()
        home = FakeTab(
            elements={
                selectors.SEARCH_INPUT.drission_locator: search_input,
            },
            element_lists={
                selectors.PRODUCT_LIST_ITEMS.drission_locator: [card],
            },
            url="https://www.taobao.com/",
        )
        original_input = search_input.input

        def navigate_in_same_tab(value: str) -> None:
            original_input(value)
            home.url = "https://s.taobao.com/search?q=陶瓷"

        search_input.input = navigate_in_same_tab
        browser = FakeBrowser(
            home_tab=home,
            tabs={"main": home},
            tab_ids=[],
        )
        collector = self.collector_class()(
            browser,
            run_id="run-same-tab-results",
            behavior_policy=base.ReadOnlyBehaviorPolicy(enabled=False),
        )

        result = collector.collect_products(
            keyword="陶瓷",
            product_limit=1,
            comment_limit=1,
        )

        self.assertEqual(len(result.products), 1)
        self.assertEqual(len(result.comments), 1)
        self.assertFalse(home.closed)
        self.assertEqual(browser.wait.timeouts, [])

    def test_product_search_detects_tab_opened_during_input(self) -> None:
        search_input = FakeElement()
        home = FakeTab(
            elements={
                selectors.SEARCH_INPUT.drission_locator: search_input,
            },
            url="https://www.taobao.com/",
        )
        results = FakeTab(url="https://s.taobao.com/search?q=陶瓷")
        browser = FakeBrowser(
            home_tab=home,
            tabs={"main": home},
            tab_ids=[],
        )
        original_input = search_input.input

        def open_during_input(value: str) -> None:
            original_input(value)
            browser.tabs["products"] = results

        search_input.input = open_during_input
        collector = self.collector_class()(
            browser,
            run_id="run-tab-during-input",
        )

        actual = collector._open_product_results(search_input, "陶瓷")

        self.assertIs(actual, results)
        self.assertEqual(browser.wait.timeouts, [])

    def test_new_tab_wait_uses_configured_timeout(self) -> None:
        card = make_product_card(
            product_id="bounded-new-tab",
            name="受限等待商品",
            price="50",
            sales="已售100",
            detail_tab=make_detail(
                20,
                [[make_comment("样例用户", "白色", "样例评论")]],
            ),
        )
        browser, _, _ = make_product_browser([card])
        collector = self.collector_class()(
            browser,
            run_id="run-bounded-new-tab",
            wait_policy=base.WaitPolicy(timeout=7, poll_interval=0.1),
            behavior_policy=base.ReadOnlyBehaviorPolicy(enabled=False),
        )

        collector.collect_products(
            keyword="陶瓷",
            product_limit=1,
            comment_limit=1,
        )

        self.assertEqual(browser.wait.timeouts, [7])

    def test_failed_tab_snapshot_never_closes_preexisting_tabs(
        self,
    ) -> None:
        card = make_product_card(
            product_id="snapshot-failure",
            name="快照失败商品",
            price="30",
            sales="30人付款",
        )
        browser, _, results_tab = make_product_browser([card])
        main_tab = browser.latest_tab
        external_tab = FakeTab(url="https://example.com/external")
        orphan_tab = FakeTab(url="https://item.taobao.com/orphan")
        browser.tabs["main"] = main_tab
        browser.tabs["external"] = external_tab
        snapshot_reads = 0

        class SnapshotFailureBrowser(FakeBrowser):
            @property
            def tab_ids(self) -> List[str]:
                nonlocal snapshot_reads
                snapshot_reads += 1
                if snapshot_reads == 4:
                    raise RuntimeError("标签快照读取失败")
                return super().tab_ids

        flaky_browser = SnapshotFailureBrowser(
            home_tab=browser.latest_tab,
            tabs=browser.tabs,
            tab_ids=["products"],
        )

        class OrphaningClick:
            def for_new_tab(self):
                flaky_browser.tabs["orphan"] = orphan_tab
                raise TimeoutError("等待详情标签超时")

        card.click = OrphaningClick()
        collector = self.collector_class()(
            flaky_browser,
            run_id="run-snapshot-failure",
            retry_policy=base.RetryPolicy(max_retries=0),
        )

        with self.assertRaises(TimeoutError):
            collector.collect_products(
                keyword="陶瓷",
                product_limit=1,
                comment_limit=1,
            )

        self.assertFalse(main_tab.closed)
        self.assertFalse(external_tab.closed)
        self.assertFalse(orphan_tab.closed)
        self.assertTrue(results_tab.closed)

    def test_state_wait_uses_wait_policy_not_behavior_sleeper(self) -> None:
        accepted_detail = make_detail(
            20,
            [[make_comment("用户乙", "标准款", "质量很好")]],
        )
        first_page = [
            make_product_card(
                product_id="wait-low",
                name="第一页低销量商品",
                price="10",
                sales="5人付款",
            )
        ]
        second_page = [
            make_product_card(
                product_id="wait-ok",
                name="等待后的合格商品",
                price="30",
                sales="30人付款",
                detail_tab=accepted_detail,
            )
        ]
        browser, results_tab = make_async_product_browser(
            first_page,
            second_page,
        )
        behavior_pauses: List[float] = []
        poll_pauses: List[float] = []
        try:
            collector = self.collector_class()(
                browser,
                run_id="run-wait-policy",
                wait_policy=base.WaitPolicy(
                    timeout=0.02,
                    poll_interval=0.005,
                ),
                sleeper=behavior_pauses.append,
                poll_sleeper=poll_pauses.append,
            )
        except TypeError as exc:
            self.fail(f"采集器尚未支持统一等待策略：{exc}")

        result = collector.collect_products(
            keyword="陶瓷",
            product_limit=1,
            comment_limit=1,
        )

        self.assertEqual(result.products[0].product_id, "wait-ok")
        self.assertEqual(behavior_pauses, [])
        self.assertEqual(poll_pauses, [0.005])
        self.assertTrue(results_tab.wait.timeouts)
        self.assertEqual(
            set(results_tab.wait.timeouts),
            {0.02},
        )

    def test_blocked_detail_stops_without_retry_and_closes_tabs(self) -> None:
        detail = make_detail(
            20,
            [[make_comment("用户甲", "白色", "做工细致")]],
        )
        normal_close = detail.close

        def close_with_error() -> None:
            normal_close()
            raise RuntimeError("详情标签关闭失败")

        detail.close = close_with_error
        card = make_product_card(
            product_id="blocked",
            name="阻塞商品",
            price="30",
            sales="30人付款",
            detail_tab=detail,
        )
        browser, _, results_tab = make_product_browser([card])

        class BlockingGuard:
            def __init__(self) -> None:
                self.steps: List[str] = []

            def inspect(self, tab, *, run_id, engine, step) -> None:
                self.steps.append(step)
                if step == "product_detail":
                    raise base.CollectionBlockedError(
                        step=step,
                        reason="检测到安全验证",
                        screenshot_path=None,
                    )

        guard = BlockingGuard()
        pauses: List[float] = []
        try:
            collector = self.collector_class()(
                browser,
                run_id="run-blocked",
                block_guard=guard,
                sleeper=pauses.append,
            )
        except TypeError as exc:
            self.fail(f"采集器尚未支持阻塞守卫：{exc}")

        with self.assertRaises(base.CollectionBlockedError):
            collector.collect_products(
                keyword="陶瓷",
                product_limit=1,
                comment_limit=1,
            )

        self.assertIn("product_detail", guard.steps)
        self.assertEqual(card.click.count, 1)
        self.assertEqual(pauses, [])
        self.assertTrue(detail.closed)
        self.assertTrue(results_tab.closed)

    def test_enabled_filter_drops_product_with_too_few_comments(self) -> None:
        detail = make_detail(19)
        card = make_product_card(
            product_id="3001",
            name="评论不足商品",
            price="30",
            sales="30人付款",
            detail_tab=detail,
        )
        browser, _, results_tab = make_product_browser([card])
        collector = self.collector_class()(
            browser,
            run_id="run-comment-filter",
        )

        result = collector.collect_products(
            keyword="陶瓷",
            product_limit=1,
            comment_limit=1,
        )

        self.assertEqual(result.products, ())
        self.assertEqual(result.comments, ())
        self.assertTrue(detail.closed)
        self.assertTrue(results_tab.closed)

    def test_product_tabs_close_when_comment_parsing_fails(self) -> None:
        invalid_comment = FakeElement(
            children={
                selectors.COMMENT_USER_NAME.drission_locator:
                    FakeElement(text="用户甲"),
                selectors.COMMENT_SKU.drission_locator:
                    FakeElement(text="规格：白色"),
            }
        )
        detail = make_detail(20, [[invalid_comment]])
        card = make_product_card(
            product_id="4001",
            name="异常评论商品",
            price="30",
            sales="30人付款",
            detail_tab=detail,
        )
        browser, _, results_tab = make_product_browser([card])
        collector = self.collector_class()(
            browser,
            run_id="run-comment-error",
            retry_policy=base.RetryPolicy(max_retries=0),
        )

        with self.assertRaises(KeyError):
            collector.collect_products(
                keyword="陶瓷",
                product_limit=1,
                comment_limit=1,
            )

        self.assertTrue(detail.closed)
        self.assertTrue(results_tab.closed)

    def test_live_flow_returns_normalized_record_and_closes_tabs(self) -> None:
        browser, search_input, results_tab, detail = make_live_browser(
            goods_title="全部商品（25）",
            link_href=(
                "https://tbzb.taobao.com/live?liveSource=pc_live.search"
                "&liveId=live-100"
            ),
        )
        collector = self.collector_class()(
            browser,
            run_id="run-live",
        )

        result = collector.collect_live(
            keyword="德化瓷",
            live_limit=1,
        )

        self.assertEqual(search_input.input_values, ["德化瓷\n"])
        self.assertEqual(len(result.live_rooms), 1)
        record = result.live_rooms[0]
        self.assertEqual(record.live_room_id, "live-100")
        self.assertEqual(record.viewer_count, 32000)
        self.assertEqual(record.follower_count, 86000)
        self.assertEqual(record.product_count, 25)
        self.assertTrue(detail.closed)
        self.assertTrue(results_tab.closed)

    def test_live_behavior_waits_and_scrolls_before_closing_room(self) -> None:
        browser, _, _, detail = make_live_browser(
            goods_title="全部商品（25）",
        )
        pauses: List[float] = []
        collector = self.collector_class()(
            browser,
            run_id="run-live-behavior",
            live_behavior_policy=base.LiveBehaviorPolicy(
                enabled=True,
                min_pause=2.0,
                max_pause=3.0,
                room_min_pause=4.0,
                room_max_pause=4.0,
                max_scrolls=2,
            ),
            sleeper=pauses.append,
        )

        collector.collect_live(keyword="德化瓷", live_limit=1)

        self.assertEqual(detail.scroll.down_count, 2)
        self.assertEqual(len(pauses), 4)
        for pause in pauses[:3]:
            self.assertGreaterEqual(pause, 2.0)
            self.assertLessEqual(pause, 3.0)
        self.assertEqual(pauses[-1], 4.0)

    def test_live_behavior_rechecks_blocking_after_pause(self) -> None:
        browser, _, results_tab, detail = make_live_browser(
            goods_title="全部商品（25）",
        )

        class BlockGuard:
            def inspect(self, tab, *, run_id, engine, step) -> None:
                if "punish" in tab.url:
                    raise base.CollectionBlockedError(
                        step=step,
                        reason="进入访问限制页面",
                        screenshot_path=None,
                    )

        def pause(delay: float) -> None:
            detail.url = "https://punish.taobao.com/"

        collector = self.collector_class()(
            browser,
            run_id="run-live-block-after-pause",
            live_behavior_policy=base.LiveBehaviorPolicy(
                enabled=True,
                min_pause=2.0,
                max_pause=3.0,
                max_scrolls=2,
            ),
            sleeper=pause,
            block_guard=BlockGuard(),
        )

        with self.assertRaises(base.CollectionBlockedError):
            collector.collect_live(keyword="德化瓷", live_limit=1)

        self.assertEqual(detail.scroll.down_count, 0)
        self.assertTrue(detail.closed)
        self.assertTrue(results_tab.closed)

    def test_login_waiter_runs_before_block_guard(self) -> None:
        browser, _, _, _ = make_live_browser(
            goods_title="全部商品（25）",
        )
        home = browser.latest_tab
        calls = []

        class LoginWaiter:
            def wait(self, tab, *, run_id, engine, step) -> None:
                calls.append(("login", step))
                tab.url = "https://www.taobao.com/"

        class BlockGuard:
            def inspect(self, tab, *, run_id, engine, step) -> None:
                calls.append(("block", step))
                if "login.taobao.com" in tab.url:
                    raise base.CollectionBlockedError(
                        step=step,
                        reason="仍在登录页",
                        screenshot_path=None,
                    )

        def redirect_to_login(url: str) -> None:
            home.url = "https://login.taobao.com/member/login.jhtml"

        home.get = redirect_to_login
        collector = self.collector_class()(
            browser,
            run_id="run-login-wait",
            login_waiter=LoginWaiter(),
            block_guard=BlockGuard(),
        )

        collector.collect_live(keyword="德化瓷", live_limit=1)

        self.assertEqual(calls[0], ("login", "home"))
        self.assertEqual(calls[1], ("block", "home"))

    def test_live_tabs_close_when_goods_count_is_abnormal(self) -> None:
        browser, _, results_tab, detail = make_live_browser(
            goods_title="全部商品（很多）",
        )
        collector = self.collector_class()(
            browser,
            run_id="run-live-error",
        )

        with self.assertRaises(parsers.NumericParseError):
            collector.collect_live(keyword="德化瓷", live_limit=1)

        self.assertTrue(detail.closed)
        self.assertTrue(results_tab.closed)

    def test_live_failure_captures_detail_before_tabs_close(self) -> None:
        browser, _, results_tab, detail = make_live_browser(
            goods_title="全部商品（很多）",
        )

        class RecordingStore:
            def __init__(self) -> None:
                self.tabs = []

            def capture(self, tab, *, run_id, engine, step):
                self.tabs.append(tab)
                return Path("detail-evidence.png")

        class Guard:
            def __init__(self) -> None:
                self.screenshot_store = RecordingStore()

            def inspect(self, tab, *, run_id, engine, step) -> None:
                pass

        guard = Guard()
        collector = self.collector_class()(
            browser,
            run_id="run-live-evidence",
            block_guard=guard,
        )

        with self.assertRaises(parsers.NumericParseError):
            collector.collect_live(keyword="德化瓷", live_limit=1)

        self.assertEqual(guard.screenshot_store.tabs, [detail])
        self.assertEqual(
            collector.last_failure_screenshot,
            Path("detail-evidence.png"),
        )
        self.assertTrue(detail.closed)
        self.assertTrue(results_tab.closed)

    def test_live_record_uses_detail_url_when_link_has_no_href(self) -> None:
        browser, _, _, detail = make_live_browser(
            goods_title="全部商品（25）",
            link_href="",
        )
        collector = self.collector_class()(
            browser,
            run_id="run-live-url",
        )

        result = collector.collect_live(
            keyword="德化瓷",
            live_limit=1,
        )

        record = result.live_rooms[0]
        self.assertEqual(record.source_url, detail.url)
        self.assertEqual(record.live_room_id, "live-100")


if __name__ == "__main__":
    unittest.main()
