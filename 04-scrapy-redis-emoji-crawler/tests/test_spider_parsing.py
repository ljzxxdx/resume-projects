import json
import unittest
from pathlib import Path
from unittest.mock import patch, sentinel

from scrapy.downloadermiddlewares.offsite import OffsiteMiddleware
from scrapy.http import HtmlResponse

from scrapyWithRedis.spiders import dmoz as dmoz_module
from scrapyWithRedis.spiders import mycrawler_redis as crawler_module
from scrapyWithRedis.spiders import myspider_redis as redis_spider_module


FIXTURES_DIR = Path(__file__).with_name("fixtures")


class SharedSpiderParsingTests(unittest.TestCase):
    def setUp(self):
        self.response = HtmlResponse(
            url="https://fabiaoqing.com/biaoqing/page/1.html",
            body=b"<html></html>",
            encoding="utf-8",
        )

    def test_all_spiders_delegate_item_extraction_to_shared_parser(self):
        cases = (
            (dmoz_module, dmoz_module.DmozSpider()),
            (redis_spider_module, redis_spider_module.MySpider()),
            (crawler_module, crawler_module.MycrawlerRedisSpider()),
        )

        for module, spider in cases:
            with self.subTest(spider=spider.name):
                self.assertTrue(hasattr(module, "extract_emoji_items"))
                self.assertTrue(hasattr(spider, "parse_emoji_image"))

                with patch.object(
                    module,
                    "extract_emoji_items",
                    return_value=iter([sentinel.item]),
                ) as shared_parser:
                    output = list(spider.parse_emoji_image(self.response))

                self.assertIn(sentinel.item, output)
                shared_parser.assert_called_once()
                called_response, called_logger = shared_parser.call_args.args
                self.assertIs(called_response, self.response)
                self.assertIs(called_logger.extra["spider"], spider)

    def test_crawl_spider_rules_use_shared_callback_name(self):
        self.assertEqual(
            dmoz_module.DmozSpider.rules[0].callback,
            "parse_emoji_image",
        )

    def test_crawl_spider_rules_follow_all_links_in_pagination_area(self):
        html = (FIXTURES_DIR / "emoji_listing.html").read_bytes()
        response = HtmlResponse(
            url="https://fabiaoqing.com/biaoqing/lists/page/1.html",
            body=html,
            encoding="utf-8",
        )
        expected_urls = [
            f"https://fabiaoqing.com/biaoqing/lists/page/{page}.html"
            for page in range(1, 5)
        ]

        for spider_class in (
            dmoz_module.DmozSpider,
            crawler_module.MycrawlerRedisSpider,
        ):
            with self.subTest(spider=spider_class.name):
                links = spider_class.rules[0].link_extractor.extract_links(
                    response
                )

                self.assertEqual(
                    [link.url for link in links],
                    expected_urls,
                )
                self.assertNotIn(
                    "https://fabiaoqing.com/biaoqing/lists/page/999.html",
                    [link.url for link in links],
                )

    def test_dmoz_accepts_multiple_normalized_start_urls(self):
        spider = dmoz_module.DmozSpider(
            start_urls_json=json.dumps(
                [
                    "https://example.com/page/2?b=2&a=1#fragment",
                    "https://example.com/page/2?a=1&b=2",
                    "https://example.com/page/3",
                ]
            )
        )

        self.assertFalse(spider.redis_start)
        self.assertEqual(
            spider.start_urls,
            [
                "https://example.com/page/2?a=1&b=2",
                "https://example.com/page/3",
            ],
        )

    def test_dmoz_keeps_legacy_single_start_url_argument(self):
        spider = dmoz_module.DmozSpider(
            start_url="https://example.com/page/1#fragment"
        )

        self.assertEqual(
            spider.start_urls,
            ["https://example.com/page/1"],
        )

    def test_dmoz_rejects_direct_urls_in_redis_mode(self):
        with self.assertRaisesRegex(ValueError, "Redis"):
            dmoz_module.DmozSpider(
                start_url="https://example.com/page/1",
                redis_start="true",
            )

    def test_dmoz_rejects_non_list_start_urls_json(self):
        with self.assertRaisesRegex(ValueError, "JSON array"):
            dmoz_module.DmozSpider(
                start_urls_json=json.dumps(
                    {"url": "https://example.com/page/1"}
                )
            )
        self.assertEqual(
            crawler_module.MycrawlerRedisSpider.rules[0].callback,
            "parse_emoji_image",
        )

    def test_redis_crawl_spider_parses_replayed_list_page_start_url(self):
        spider = crawler_module.MycrawlerRedisSpider()
        with patch.object(
            crawler_module,
            "extract_emoji_items",
            return_value=iter([sentinel.item]),
        ) as shared_parser:
            output = list(spider.parse_start_url(self.response))

        self.assertEqual(output, [sentinel.item])
        shared_parser.assert_called_once()
        called_response, called_logger = shared_parser.call_args.args
        self.assertIs(called_response, self.response)
        self.assertIs(called_logger.extra["spider"], spider)

    def test_dmoz_parses_direct_or_redis_list_page_start_url(self):
        spider = dmoz_module.DmozSpider()
        with patch.object(
            dmoz_module,
            "extract_emoji_items",
            return_value=iter([sentinel.item]),
        ) as shared_parser:
            output = list(spider.parse_start_url(self.response))

        self.assertEqual(output, [sentinel.item])
        shared_parser.assert_called_once()
        called_response, called_logger = shared_parser.call_args.args
        self.assertIs(called_response, self.response)
        self.assertIs(called_logger.extra["spider"], spider)

    def test_redis_spiders_allow_all_domains_when_domain_is_omitted(self):
        for spider in (
            redis_spider_module.MySpider(),
            crawler_module.MycrawlerRedisSpider(),
        ):
            with self.subTest(spider=spider.name):
                self.assertEqual(spider.allowed_domains, [])
                host_regex = OffsiteMiddleware.get_host_regex(None, spider)
                self.assertIsNotNone(host_regex.search("127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
