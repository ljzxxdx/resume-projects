"""Tests for the shared CSS/XPath selector registry."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from taobao_collector import selectors


class SelectorRegistryTests(unittest.TestCase):
    def test_xpath_selector_supports_both_engines(self) -> None:
        selector = selectors.PRODUCT_TITLE

        self.assertEqual(selector.strategy, selectors.SelectorStrategy.XPATH)
        self.assertEqual(
            selector.drission_locator,
            'xpath://div[starts-with(@class,"title")]',
        )
        self.assertEqual(
            selector.selenium_locator,
            ("xpath", '//div[starts-with(@class,"title")]'),
        )

    def test_css_selector_supports_both_engines(self) -> None:
        selector = selectors.SEARCH_INPUT

        self.assertEqual(selector.strategy, selectors.SelectorStrategy.CSS)
        self.assertEqual(
            selector.drission_locator,
            "css:.search-suggest-combobox-imageSearch-input",
        )
        self.assertEqual(
            selector.selenium_locator,
            (
                "css selector",
                ".search-suggest-combobox-imageSearch-input",
            ),
        )

    def test_selector_requires_name_query_and_purpose(self) -> None:
        invalid_values = (
            ("", "//div", "读取内容"),
            ("item", "", "读取内容"),
            ("item", "//div", ""),
            ("item", "   ", "读取内容"),
        )

        for name, query, purpose in invalid_values:
            with self.subTest(name=name, query=query, purpose=purpose):
                with self.assertRaises(ValueError):
                    selectors.Selector(
                        name=name,
                        strategy=selectors.SelectorStrategy.XPATH,
                        query=query,
                        purpose=purpose,
                    )

    def test_registry_names_are_unique_and_purposes_are_complete(self) -> None:
        names = [selector.name for selector in selectors.ALL_SELECTORS]

        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(
            set(names),
            set(selectors.SELECTORS_BY_NAME),
        )
        for selector in selectors.ALL_SELECTORS:
            with self.subTest(name=selector.name):
                self.assertTrue(selector.purpose.strip())
                self.assertTrue(selector.query.strip())
                self.assertIs(
                    selectors.SELECTORS_BY_NAME[selector.name],
                    selector,
                )

    def test_registry_covers_product_comment_and_live_fields(self) -> None:
        expected_names = {
            "search_input",
            "product_list_items",
            "product_title",
            "product_price",
            "product_sales",
            "next_page_button",
            "detail_title",
            "comment_open_button",
            "comment_drawer",
            "comment_items",
            "comment_user_name",
            "comment_sku",
            "comment_content",
            "live_nav_tab",
            "live_search_input",
            "live_list_items",
            "live_link_button",
            "live_account_name",
            "live_introduction",
            "live_info_counts",
            "live_goods_title",
        }

        self.assertTrue(
            expected_names.issubset(selectors.SELECTORS_BY_NAME)
        )

    def test_registry_excludes_account_side_effect_selectors(self) -> None:
        forbidden_terms = (
            "collectbtn",
            "taobaojiarugouwuche",
            "收藏",
            "加购",
            "购物车",
        )

        for selector in selectors.ALL_SELECTORS:
            searchable = (
                f"{selector.name} {selector.query} {selector.purpose}"
            ).lower()
            for term in forbidden_terms:
                with self.subTest(name=selector.name, term=term):
                    self.assertNotIn(term.lower(), searchable)

    def test_selector_and_registry_are_immutable(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            selectors.PRODUCT_TITLE.purpose = "修改后的含义"

        with self.assertRaises(TypeError):
            selectors.SELECTORS_BY_NAME["new"] = selectors.PRODUCT_TITLE


if __name__ == "__main__":
    unittest.main()
