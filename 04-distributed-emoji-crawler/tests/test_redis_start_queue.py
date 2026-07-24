import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scrapy import Request
from scrapy.exceptions import DontCloseSpider
from scrapy.settings import Settings

from scrapyWithRedis.spiders import dmoz as dmoz_module

try:
    from scrapyWithRedis.redis_start_queue import RedisStartQueueConsumer
except ModuleNotFoundError:
    RedisStartQueueConsumer = None


class FakePipeline:
    def __init__(self, server):
        self.server = server
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def lrange(self, key, start, end):
        self.commands.append(("lrange", key, start, end))
        return self

    def ltrim(self, key, start, end):
        self.commands.append(("ltrim", key, start, end))
        return self

    def execute(self):
        original = {
            key: list(values) for key, values in self.server.lists.items()
        }
        results = []
        for command, key, start, end in self.commands:
            values = original.get(key, [])
            if command == "lrange":
                results.append(values[start:end + 1])
            else:
                self.server.lists[key] = values[start:]
                results.append(True)
        return results


class FakeRedis:
    def __init__(self):
        self.lists = {}

    def pipeline(self):
        return FakePipeline(self)

    def llen(self, key):
        return len(self.lists.get(key, []))


class FakeSignals:
    def __init__(self):
        self.connections = []

    def connect(self, receiver, signal):
        self.connections.append((receiver, signal))


class FakeEngine:
    def __init__(self):
        self.requests = []

    def crawl(self, request):
        self.requests.append(request)


def make_crawler(**overrides):
    values = {
        "REDIS_START_URLS_KEY": "project:%(name)s:start_urls",
        "CONCURRENT_REQUESTS": 2,
        "DMOZ_REDIS_MAX_IDLE_TIME": 30,
    }
    values.update(overrides)
    return SimpleNamespace(
        settings=Settings(values),
        signals=FakeSignals(),
        engine=FakeEngine(),
    )


class RedisStartQueueConsumerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            RedisStartQueueConsumer,
            "RedisStartQueueConsumer must be implemented",
        )

    def make_consumer(self, server, **settings):
        crawler = make_crawler(**settings)
        spider = SimpleNamespace(name="dmoz", logger=SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda *args, **kwargs: None,
            debug=lambda *args, **kwargs: None,
        ))
        with patch(
            "scrapyWithRedis.redis_start_queue.connection.from_settings",
            return_value=server,
        ):
            consumer = RedisStartQueueConsumer(crawler, spider)
        return consumer, crawler

    def test_claims_one_configured_batch_and_leaves_the_remainder(self):
        server = FakeRedis()
        key = "project:dmoz:start_urls"
        server.lists[key] = [
            json.dumps({"url": f"https://example.com/{index}"}).encode()
            for index in range(1, 4)
        ]
        consumer, _ = self.make_consumer(server)

        requests = list(consumer.next_requests())

        self.assertEqual(
            [request.url for request in requests],
            ["https://example.com/1", "https://example.com/2"],
        )
        self.assertTrue(all(request.dont_filter for request in requests))
        self.assertEqual(len(server.lists[key]), 1)

    def test_rejects_batch_size_above_fifty(self):
        with self.assertRaisesRegex(ValueError, "1 through 50"):
            self.make_consumer(
                FakeRedis(),
                DMOZ_REDIS_BATCH_SIZE=51,
            )

    def test_waits_during_grace_period_then_allows_close(self):
        consumer, _ = self.make_consumer(FakeRedis())

        with patch("scrapy_redis.spiders.time.time", return_value=100):
            consumer.spider_idle_start_time = 90
            with self.assertRaises(DontCloseSpider):
                consumer.spider_idle()

            consumer.spider_idle_start_time = 70
            self.assertIsNone(consumer.spider_idle())

    def test_idle_consumer_schedules_new_batch_and_stays_open(self):
        server = FakeRedis()
        key = "project:dmoz:start_urls"
        server.lists[key] = [
            json.dumps({"url": "https://example.com/new"}).encode()
        ]
        consumer, crawler = self.make_consumer(server)

        with patch("scrapy_redis.spiders.time.time", return_value=100):
            with self.assertRaises(DontCloseSpider):
                consumer.spider_idle()

        self.assertEqual(
            [request.url for request in crawler.engine.requests],
            ["https://example.com/new"],
        )


class DmozRedisStartModeTests(unittest.TestCase):
    def test_from_crawler_builds_consumer_only_in_redis_mode(self):
        crawler = make_crawler()
        sentinel_consumer = object()
        with patch.object(
            dmoz_module,
            "RedisStartQueueConsumer",
            return_value=sentinel_consumer,
        ) as consumer_class:
            redis_spider = dmoz_module.DmozSpider.from_crawler(
                crawler,
                redis_start="true",
            )
            direct_spider = dmoz_module.DmozSpider.from_crawler(crawler)

        self.assertIs(redis_spider.redis_start_queue, sentinel_consumer)
        self.assertIsNone(direct_spider.redis_start_queue)
        consumer_class.assert_called_once_with(crawler, redis_spider)

    def test_redis_start_yields_only_consumer_requests(self):
        spider = dmoz_module.DmozSpider(redis_start="true")
        expected = Request("https://example.com/from-redis")
        spider.redis_start_queue = SimpleNamespace(
            next_requests=lambda: iter([expected])
        )

        async def collect_start_requests():
            return [request async for request in spider.start()]

        requests = asyncio.run(collect_start_requests())

        self.assertEqual(requests, [expected])


if __name__ == "__main__":
    unittest.main()
