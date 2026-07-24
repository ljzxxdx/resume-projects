import unittest
from types import SimpleNamespace

from scrapy_redis.pipelines import RedisPipeline

from scrapyWithRedis import settings
from scrapyWithRedis.spiders.mycrawler_redis import MycrawlerRedisSpider
from scrapyWithRedis.spiders.myspider_redis import MySpider


class RedisKeyNamespaceTests(unittest.TestCase):
    def test_declares_one_namespace_for_every_redis_data_role(self):
        expected = {
            "REDIS_START_URLS_KEY": (
                "scrapyWithRedis:%(name)s:start_urls"
            ),
            "SCHEDULER_QUEUE_KEY": (
                "scrapyWithRedis:%(spider)s:requests"
            ),
            "SCHEDULER_DUPEFILTER_KEY": (
                "scrapyWithRedis:dupe:requests"
            ),
            "ITEM_URL_DUPE_KEY": (
                "scrapyWithRedis:dupe:item_urls"
            ),
            "REDIS_ITEMS_KEY": (
                "scrapyWithRedis:%(spider)s:items"
            ),
            "PAGE_FAILURE_QUEUE_KEY": (
                "scrapyWithRedis:%(spider)s:failures:pages"
            ),
            "PAGE_FAILURE_DUPE_KEY": (
                "scrapyWithRedis:%(spider)s:failures:pages:dupe"
            ),
            "IMAGE_FAILURE_QUEUE_KEY": (
                "scrapyWithRedis:%(spider)s:failures:images"
            ),
            "IMAGE_FAILURE_DUPE_KEY": (
                "scrapyWithRedis:failures:images:dupe"
            ),
            "IMAGE_RETRY_QUEUE_KEY": (
                "scrapyWithRedis:image_retry:start_urls"
            ),
        }

        actual = {
            name: getattr(settings, name, None)
            for name in expected
        }

        self.assertEqual(actual, expected)

    def test_key_roles_resolve_to_distinct_concrete_keys(self):
        spider_name = "mycrawler_redis"
        concrete_keys = {
            settings.REDIS_START_URLS_KEY % {"name": spider_name},
            settings.SCHEDULER_QUEUE_KEY % {"spider": spider_name},
            settings.SCHEDULER_DUPEFILTER_KEY,
            settings.ITEM_URL_DUPE_KEY,
            settings.REDIS_ITEMS_KEY % {"spider": spider_name},
            settings.PAGE_FAILURE_QUEUE_KEY % {"spider": spider_name},
            settings.PAGE_FAILURE_DUPE_KEY % {"spider": spider_name},
            settings.IMAGE_FAILURE_QUEUE_KEY % {"spider": spider_name},
            settings.IMAGE_FAILURE_DUPE_KEY,
            settings.IMAGE_RETRY_QUEUE_KEY,
        }

        self.assertEqual(len(concrete_keys), 10)
        self.assertTrue(
            all(key.startswith("scrapyWithRedis:") for key in concrete_keys)
        )

    def test_redis_spiders_use_the_shared_start_queue_template(self):
        self.assertIsNone(MySpider.redis_key)
        self.assertIsNone(MycrawlerRedisSpider.redis_key)

        self.assertEqual(
            settings.REDIS_START_URLS_KEY
            % {"name": MySpider.name},
            "scrapyWithRedis:myspider_redis:start_urls",
        )
        self.assertEqual(
            settings.REDIS_START_URLS_KEY
            % {"name": MycrawlerRedisSpider.name},
            "scrapyWithRedis:mycrawler_redis:start_urls",
        )

    def test_redis_pipeline_uses_the_namespaced_result_list(self):
        pipeline = RedisPipeline(
            server=None,
            key=settings.REDIS_ITEMS_KEY,
        )

        key = pipeline.item_key(
            item=None,
            spider=SimpleNamespace(name="dmoz"),
        )

        self.assertEqual(key, "scrapyWithRedis:dmoz:items")


if __name__ == "__main__":
    unittest.main()
