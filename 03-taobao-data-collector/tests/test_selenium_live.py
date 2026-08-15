"""Selenium 直播卡片与详情字段解析测试。"""

from __future__ import annotations

import importlib
import unittest
from contextlib import contextmanager

from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.common.keys import Keys

from taobao_collector import selectors
from taobao_collector.collectors.base import (
    CollectionResult,
    LiveBehaviorPolicy,
)
from taobao_collector.models import Engine, LiveRoomRecord


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
        self.clear_count = 0
        self.sent_keys = []

    def get_attribute(self, name: str):
        return self.attributes.get(name)

    def find_element(self, by, query):
        locator = (by, query)
        if locator not in self.children:
            raise NoSuchElementException(f"未找到元素：{locator!r}")
        return self.children[locator]

    def find_elements(self, by, query):
        return list(self.child_lists.get((by, query), ()))

    def clear(self) -> None:
        self.clear_count += 1

    def send_keys(self, *values) -> None:
        self.sent_keys.extend(values)


class SeleniumLiveParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )

    def live_card_data_class(self):
        data_class = getattr(self.module, "_LiveCardData", None)
        self.assertIsNotNone(data_class, "_LiveCardData 尚未实现")
        return data_class

    def test_live_card_parses_identity_text_and_numeric_counts(self) -> None:
        collector = self.module.SeleniumCollector(
            driver=object(),
            run_id="run-selenium-live-card",
        )
        source_url = (
            "https://tbzb.taobao.com/live?liveSource=pc_live.search"
            "&liveId=3803425461329170"
        )
        card = FakeElement(
            children={
                selectors.LIVE_LINK_BUTTON.selenium_locator: FakeElement(
                    attributes={"href": source_url}
                ),
                selectors.LIVE_ACCOUNT_NAME.selenium_locator: FakeElement(
                    text="  陶瓷直播间  "
                ),
                selectors.LIVE_INTRODUCTION.selenium_locator: FakeElement(
                    text="  手工陶瓷制作  "
                ),
            },
            child_lists={
                selectors.LIVE_INFO_COUNTS.selenium_locator: [
                    FakeElement(text="3.6万人观看"),
                    FakeElement(text="粉丝 8.6万+"),
                ]
            },
        )

        parsed = collector._parse_live_card(card)

        self.assertEqual(parsed.live_room_id, "3803425461329170")
        self.assertEqual(parsed.source_url, source_url)
        self.assertEqual(parsed.account_name, "陶瓷直播间")
        self.assertEqual(parsed.introduction, "手工陶瓷制作")
        self.assertEqual(parsed.viewer_count, 36000)
        self.assertEqual(parsed.follower_count, 86000)

    def test_detail_product_count_and_record_use_selenium_model(self) -> None:
        title = FakeElement(text="全部商品（1.2千+）")
        driver = FakeElement(
            children={selectors.LIVE_GOODS_TITLE.selenium_locator: title}
        )
        collector = self.module.SeleniumCollector(
            driver=driver,
            run_id="run-selenium-live-record",
        )
        data_class = self.live_card_data_class()
        live_room = data_class(
            live_room_id="",
            source_url="",
            account_name="陶瓷主播",
            introduction="直播介绍",
            viewer_count=12000,
            follower_count=500,
        )
        detail_url = (
            "https://liveplatform.taobao.com/live/liveDetail.htm"
            "?roomId=room-fallback"
        )

        product_count = collector._parse_live_product_count()
        record = collector._build_live_record(
            live_room,
            "陶瓷",
            product_count,
            detail_url,
        )

        self.assertEqual(product_count, 1200)
        self.assertIsInstance(record, LiveRoomRecord)
        self.assertEqual(record.engine, Engine.SELENIUM)
        self.assertEqual(record.run_id, "run-selenium-live-record")
        self.assertEqual(record.keyword, "陶瓷")
        self.assertEqual(record.live_room_id, "room-fallback")
        self.assertEqual(record.source_url, detail_url)
        self.assertEqual(record.product_count, 1200)


class SeleniumLiveLoadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )

    def test_live_search_clears_input_and_submits_with_enter(self) -> None:
        collector = self.module.SeleniumCollector(
            driver=object(),
            run_id="run-selenium-live-search",
        )
        search_input = FakeElement()

        collector._submit_live_search(search_input, "陶瓷")

        self.assertEqual(search_input.clear_count, 1)
        self.assertEqual(search_input.sent_keys, ["陶瓷", Keys.ENTER])

    def test_live_cards_load_until_limit_with_bounded_scrolls(self) -> None:
        module = self.module
        first, second, third = object(), object(), object()

        class LoadingCollector(module.SeleniumCollector):
            def __init__(self, snapshots):
                super().__init__(
                    driver=object(),
                    run_id="run-selenium-live-loading",
                    live_behavior_policy=LiveBehaviorPolicy(
                        enabled=False,
                        max_scrolls=2,
                    ),
                )
                self.snapshots = [list(items) for items in snapshots]
                self.index = 0
                self.scroll_count = 0

            def _wait_live_cards(self):
                return self.snapshots[self.index]

            def _scroll_page(self, distance=400) -> None:
                self.scroll_count += 1
                if self.index < len(self.snapshots) - 1:
                    self.index += 1

            def _wait_for_more_elements(
                self,
                selector,
                *,
                previous_count,
                root=None,
            ):
                cards = self.snapshots[self.index]
                if len(cards) <= previous_count:
                    raise TimeoutException("直播卡片数量未增长")
                return cards

        growing = LoadingCollector(
            [[first], [first, second], [first, second, third]]
        )
        unchanged = LoadingCollector([[first], [first]])

        self.assertEqual(
            list(growing._load_live_cards(live_limit=3)),
            [first, second, third],
        )
        self.assertEqual(growing.scroll_count, 2)
        self.assertEqual(
            list(unchanged._load_live_cards(live_limit=3)),
            [first],
        )
        self.assertEqual(unchanged.scroll_count, 1)


class SeleniumLiveCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = importlib.import_module(
            "taobao_collector.collectors.selenium_collector"
        )

    def test_collection_validates_keyword_and_limit_before_navigation(
        self,
    ) -> None:
        collector = self.module.SeleniumCollector(
            driver=object(),
            run_id="run-selenium-live-validation",
        )

        for arguments in (
            {"keyword": " ", "live_limit": 1},
            {"keyword": "陶瓷", "live_limit": 0},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    collector.collect_live(**arguments)

    def test_collection_uses_nested_tabs_and_returns_live_records(self) -> None:
        module = self.module
        data_class = getattr(module, "_LiveCardData")
        navigation = object()
        detail_link = FakeElement(
            attributes={"href": "https://live/detail?roomId=room-9"}
        )
        card = FakeElement(
            children={
                selectors.LIVE_LINK_BUTTON.selenium_locator: detail_link
            }
        )

        class FlowDriver:
            current_url = "https://www.taobao.com/"

        class FlowCollector(module.SeleniumCollector):
            def __init__(self):
                super().__init__(
                    driver=FlowDriver(),
                    run_id="run-selenium-live-flow",
                )
                self.depth = 0
                self.events = []
                self.clicked = []
                self.search_terms = []

            def _open_home(self) -> None:
                self.events.append("home")

            def _check_block(self, step: str) -> None:
                self.events.append(f"check-{step}")

            def _wait_live_navigation(self):
                return navigation

            def _move_and_click(self, element) -> None:
                self.clicked.append(element)

            @contextmanager
            def _temporary_new_tab(self, opener):
                original_url = self.driver.current_url
                opener()
                self.depth += 1
                self.events.append(f"enter-{self.depth}")
                self.driver.current_url = (
                    "https://live/results"
                    if self.depth == 1
                    else "https://live/detail?roomId=room-9"
                )
                try:
                    yield f"tab-{self.depth}"
                finally:
                    self.events.append(f"exit-{self.depth}")
                    self.depth -= 1
                    self.driver.current_url = original_url

            def _wait_live_search_input(self):
                return object()

            def _submit_live_search(self, search_input, keyword) -> None:
                self.search_terms.append(keyword)

            def _load_live_cards(self, *, live_limit):
                self.asserted_limit = live_limit
                return [card]

            @staticmethod
            def _parse_live_card(item):
                return data_class(
                    live_room_id="room-9",
                    source_url="https://live/detail?roomId=room-9",
                    account_name="陶瓷主播",
                    introduction="手工陶瓷直播",
                    viewer_count=36000,
                    follower_count=86000,
                )

            def _wait_live_detail(self):
                self.events.append("detail-ready")

            def _perform_live_read_only_behavior(self) -> None:
                self.events.append("detail-behavior")

            def _parse_live_product_count(self):
                return 25

            def _pace_live_room_transition(self) -> None:
                self.events.append("room-pause")

        collector = FlowCollector()
        result = collector.collect_live(keyword="  陶瓷  ", live_limit=1)

        self.assertIsInstance(result, CollectionResult)
        self.assertEqual(collector.search_terms, ["陶瓷"])
        self.assertEqual(collector.asserted_limit, 1)
        self.assertEqual(collector.clicked, [navigation, detail_link])
        self.assertEqual(
            collector.events,
            [
                "home",
                "check-home",
                "enter-1",
                "check-live_results",
                "enter-2",
                "check-live_detail",
                "detail-ready",
                "detail-behavior",
                "exit-2",
                "room-pause",
                "exit-1",
            ],
        )
        self.assertEqual(collector.depth, 0)
        self.assertEqual(len(result.live_rooms), 1)
        record = result.live_rooms[0]
        self.assertEqual(record.engine, Engine.SELENIUM)
        self.assertEqual(record.live_room_id, "room-9")
        self.assertEqual(record.product_count, 25)


if __name__ == "__main__":
    unittest.main()
