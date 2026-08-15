"""脱敏 HTML 样本的双引擎字段解析契约测试。"""

from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

from taobao_collector import selectors
from taobao_collector.collectors.drission_collector import DrissionCollector
from taobao_collector.collectors.selenium_collector import SeleniumCollector
from tests.support.html_dom import (
    FixtureBrowser,
    FixtureDocument,
    FixtureSelectorMissingError,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"


class HtmlFixtureParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.product_document = FixtureDocument.from_file(
            FIXTURE_DIR / "product_card.html"
        )
        self.comment_document = FixtureDocument.from_file(
            FIXTURE_DIR / "comment_detail.html"
        )
        self.live_document = FixtureDocument.from_file(
            FIXTURE_DIR / "live_room.html"
        )
        self.home_document = FixtureDocument.from_file(
            FIXTURE_DIR / "home_nav.html"
        )

    def collectors(self, document: FixtureDocument):
        yield SeleniumCollector(driver=document, run_id="fixture-run")
        yield DrissionCollector(
            FixtureBrowser(document),
            run_id="fixture-run",
        )

    def test_product_card_fields_parse_for_both_engines(self) -> None:
        for collector in self.collectors(self.product_document):
            with self.subTest(engine=type(collector).__name__):
                card = self.product_document.required(selectors.PRODUCT_LIST_ITEMS)
                parsed = collector._parse_product_card(card)

                self.assertEqual(parsed.product_id, "sample-product-001")
                self.assertEqual(parsed.name, "脱敏青瓷茶杯")
                self.assertEqual(parsed.price, Decimal("129.90"))
                self.assertEqual(parsed.sales_count, 12000)

    def test_comment_fields_parse_for_both_engines(self) -> None:
        for collector in self.collectors(self.comment_document):
            with self.subTest(engine=type(collector).__name__):
                item = self.comment_document.required(selectors.COMMENT_ITEMS)
                product = collector._parse_product_card(
                    self.product_document.required(selectors.PRODUCT_LIST_ITEMS)
                )
                parsed = collector._parse_comment_item(item, product, "青瓷")

                self.assertEqual(parsed.user_name, "样例用户甲")
                self.assertEqual(parsed.sku_info, "青釉")
                self.assertEqual(parsed.content, "包装完整，样例内容。")

    def test_live_fields_parse_for_both_engines(self) -> None:
        for collector in self.collectors(self.live_document):
            with self.subTest(engine=type(collector).__name__):
                card = self.live_document.required(selectors.LIVE_LIST_ITEMS)
                parsed = collector._parse_live_card(card)

                self.assertEqual(parsed.live_room_id, "sample-room-001")
                self.assertEqual(parsed.account_name, "样例陶瓷馆")
                self.assertEqual(parsed.introduction, "脱敏直播介绍")
                self.assertEqual(parsed.viewer_count, 36000)
                self.assertEqual(parsed.follower_count, 86000)
                if isinstance(collector, SeleniumCollector):
                    product_count = collector._parse_live_product_count()
                else:
                    product_count = collector._parse_live_product_count(
                        self.live_document
                    )
                self.assertEqual(product_count, 25)

    def test_current_home_navigation_exposes_live_entry_for_both_engines(
        self,
    ) -> None:
        selenium_entry = self.home_document.required(
            selectors.LIVE_NAV_TAB
        )
        drission_entry = self.home_document.ele(
            selectors.LIVE_NAV_TAB.drission_locator
        )

        self.assertEqual(selenium_entry.text, "淘宝直播 红包雨")
        self.assertEqual(drission_entry.text, "淘宝直播 红包雨")

    def test_structure_change_names_the_missing_selector(self) -> None:
        changed = FixtureDocument.from_html(
            self.product_document.source.replace(
                '<div class="innerPriceWrapper--sample">¥129.90</div>',
                "",
            )
        )

        for collector in self.collectors(changed):
            with self.subTest(engine=type(collector).__name__):
                with self.assertRaisesRegex(
                    FixtureSelectorMissingError,
                    r"product_price.*读取商品价格文本.*innerPriceWrapper",
                ):
                    collector._parse_product_card(
                        changed.required(selectors.PRODUCT_LIST_ITEMS)
                    )

    def test_partial_live_counts_report_selector_and_required_count(self) -> None:
        changed = FixtureDocument.from_html(
            self.live_document.source.replace(
                '<p class="infoText--sample">粉丝8.6万+</p>',
                "",
            )
        )
        card = changed.required(selectors.LIVE_LIST_ITEMS)

        for collector in self.collectors(changed):
            with self.subTest(engine=type(collector).__name__):
                with self.assertRaisesRegex(
                    selectors.SelectorStructureError,
                    r"live_info_counts.*读取直播观看数和粉丝数.*至少需要2个.*实际1个",
                ):
                    collector._parse_live_card(card)

    def test_samples_are_synthetic_and_do_not_contain_session_data(self) -> None:
        combined = "\n".join(
            document.source
            for document in (
                self.product_document,
                self.comment_document,
                self.live_document,
                self.home_document,
            )
        ).lower()

        self.assertEqual(combined.count('data-fixture="synthetic-redacted"'), 4)
        for forbidden in ("cookie", "localstorage", "sessionstorage", "淘宝账号"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden.lower(), combined)


if __name__ == "__main__":
    unittest.main()
