import importlib
import logging
import unittest
from pathlib import Path

from scrapy.http import HtmlResponse

from scrapyWithRedis.items import EmojiItem


FIXTURES_DIR = Path(__file__).with_name("fixtures")


def make_response(html):
    return HtmlResponse(
        url="https://fabiaoqing.com/biaoqing/page/1.html",
        body=html.encode("utf-8"),
        encoding="utf-8",
    )


def load_parsers_module():
    try:
        return importlib.import_module("scrapyWithRedis.parsers")
    except ModuleNotFoundError:
        return None


class ExtractEmojiItemsTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("tests.parsers")

    def test_extracts_normalized_emoji_item(self):
        parsers = load_parsers_module()
        self.assertIsNotNone(parsers, "shared parsers module must exist")

        response = make_response(
            '<img class="ui image lazy" '
            'data-original=" /images/a.gif " alt=" demo ">'
        )

        items = list(parsers.extract_emoji_items(response, self.logger))

        self.assertEqual(len(items), 1)
        self.assertIsInstance(items[0], EmojiItem)
        self.assertEqual(items[0]["img_url"], "https://fabiaoqing.com/images/a.gif")
        self.assertEqual(items[0]["title"], "demo")
        self.assertEqual(items[0]["source_url"], response.url)

    def test_extracts_items_from_fixed_listing_fixture(self):
        parsers = load_parsers_module()
        self.assertIsNotNone(parsers, "shared parsers module must exist")
        html = (FIXTURES_DIR / "emoji_listing.html").read_text(
            encoding="utf-8"
        )
        response = make_response(html)

        with self.assertLogs(self.logger, level="WARNING") as captured:
            items = list(parsers.extract_emoji_items(response, self.logger))

        self.assertEqual(
            [dict(item) for item in items],
            [
                {
                    "img_url": "https://fabiaoqing.com/images/relative.gif",
                    "title": "relative demo",
                    "source_url": response.url,
                },
                {
                    "img_url": "https://cdn.example.com/absolute.png",
                    "title": "",
                    "source_url": response.url,
                },
            ],
        )
        logs = " ".join(captured.output)
        self.assertIn("missing img_url", logs)
        self.assertIn("missing title", logs)
        self.assertIn(response.url, logs)

    def test_skips_image_without_url_and_logs_source_url(self):
        parsers = load_parsers_module()
        self.assertIsNotNone(parsers, "shared parsers module must exist")
        response = make_response('<img class="ui image lazy" alt="missing-url">')

        with self.assertLogs(self.logger, level="WARNING") as captured:
            items = list(parsers.extract_emoji_items(response, self.logger))

        self.assertEqual(items, [])
        self.assertIn(response.url, " ".join(captured.output))

    def test_keeps_image_without_title_and_logs_source_url(self):
        parsers = load_parsers_module()
        self.assertIsNotNone(parsers, "shared parsers module must exist")
        response = make_response(
            '<img class="ui image lazy" data-original="/images/no-title.gif">'
        )

        with self.assertLogs(self.logger, level="WARNING") as captured:
            items = list(parsers.extract_emoji_items(response, self.logger))

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "")
        self.assertIn(response.url, " ".join(captured.output))


if __name__ == "__main__":
    unittest.main()
