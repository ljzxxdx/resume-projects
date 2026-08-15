"""Tests for pure numeric text parsers."""

from __future__ import annotations

import importlib
import unittest
from decimal import Decimal


class NumericParserTests(unittest.TestCase):
    def load_parsers(self):
        try:
            return importlib.import_module("taobao_collector.parsers")
        except ModuleNotFoundError:
            self.fail("taobao_collector.parsers 尚未实现")

    def test_price_keeps_decimal_precision_and_removes_display_text(self) -> None:
        parsers = self.load_parsers()

        cases = {
            "¥1,299.50": Decimal("1299.50"),
            "价格 39.9 元": Decimal("39.9"),
            "2千+": Decimal("2000"),
            "1.25万": Decimal("12500"),
        }
        for raw_text, expected in cases.items():
            with self.subTest(raw_text=raw_text):
                self.assertEqual(parsers.parse_price(raw_text), expected)

    def test_count_parsers_support_integer_decimal_unit_and_plus(self) -> None:
        parsers = self.load_parsers()

        cases = (
            (parsers.parse_sales_count, "128人付款", 128),
            (parsers.parse_sales_count, "已售1.2万+", 12000),
            (parsers.parse_comment_count, "评论 3千+", 3000),
            (parsers.parse_comment_count, "2.35万条评价", 23500),
            (parsers.parse_comment_count, "用户评价：1.2万+", 12000),
            (parsers.parse_viewer_count, "观看1,299+", 1299),
            (parsers.parse_viewer_count, "3.6万人观看", 36000),
            (parsers.parse_follower_count, "粉丝 8.6万+", 86000),
            (parsers.parse_follower_count, "500", 500),
        )
        for parser, raw_text, expected in cases:
            with self.subTest(parser=parser.__name__, raw_text=raw_text):
                try:
                    actual = parser(raw_text)
                except parsers.NumericParseError as exc:
                    self.fail(f"合法业务文本不应解析失败：{exc}")
                self.assertEqual(actual, expected)

    def test_empty_placeholders_return_none(self) -> None:
        parsers = self.load_parsers()

        parser_functions = (
            parsers.parse_price,
            parsers.parse_sales_count,
            parsers.parse_comment_count,
            parsers.parse_viewer_count,
            parsers.parse_follower_count,
        )
        for parser in parser_functions:
            for raw_text in (None, "", "   ", "--", "暂无", "N/A"):
                with self.subTest(parser=parser.__name__, raw_text=raw_text):
                    self.assertIsNone(parser(raw_text))

    def test_abnormal_text_raises_numeric_parse_error(self) -> None:
        parsers = self.load_parsers()

        parser_functions = (
            parsers.parse_price,
            parsers.parse_sales_count,
            parsers.parse_comment_count,
            parsers.parse_viewer_count,
            parsers.parse_follower_count,
        )
        for parser in parser_functions:
            for raw_text in ("很多", "1-2万", "-12"):
                with self.subTest(parser=parser.__name__, raw_text=raw_text):
                    with self.assertRaises(parsers.NumericParseError):
                        parser(raw_text)

    def test_count_without_unit_rejects_fractional_result(self) -> None:
        parsers = self.load_parsers()

        with self.assertRaises(parsers.NumericParseError):
            parsers.parse_comment_count("1.5条评价")

    def test_live_product_count_supports_title_and_parentheses(self) -> None:
        parsers = self.load_parsers()
        parser = getattr(parsers, "parse_live_product_count", None)
        self.assertIsNotNone(parser, "parse_live_product_count 尚未实现")

        cases = {
            "全部商品(25)": 25,
            "全部商品（1.2千+）": 1200,
            "商品 30 件": 30,
        }
        for raw_text, expected in cases.items():
            with self.subTest(raw_text=raw_text):
                self.assertEqual(parser(raw_text), expected)


if __name__ == "__main__":
    unittest.main()
