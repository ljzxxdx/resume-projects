import time

from scrapy import signals
from scrapy_redis import connection
from scrapy_redis.spiders import RedisMixin


class RedisStartQueueConsumer(RedisMixin):
    """Give a regular CrawlSpider scrapy-redis start-queue behavior."""

    def __init__(self, crawler, spider):
        self.crawler = crawler
        self.spider = spider
        self.name = spider.name
        self.logger = spider.logger
        self.redis_key = crawler.settings.get(
            "REDIS_START_URLS_KEY",
            "%(name)s:start_urls",
        ) % {"name": spider.name}
        self.redis_encoding = crawler.settings.get("REDIS_ENCODING", "utf-8")

        concurrent_requests = crawler.settings.getint(
            "CONCURRENT_REQUESTS",
            16,
        )
        self.redis_batch_size = crawler.settings.getint(
            "DMOZ_REDIS_BATCH_SIZE",
            min(concurrent_requests, 50),
        )
        if not 1 <= self.redis_batch_size <= 50:
            raise ValueError("DMOZ_REDIS_BATCH_SIZE must be 1 through 50")

        self.max_idle_time = crawler.settings.getint(
            "DMOZ_REDIS_MAX_IDLE_TIME",
            30,
        )
        if self.max_idle_time < 0:
            raise ValueError("DMOZ_REDIS_MAX_IDLE_TIME must not be negative")

        self.server = connection.from_settings(crawler.settings)
        self.fetch_data = self.pop_list_queue
        self.count_size = self.server.llen
        self.spider_idle_start_time = int(time.time())

        crawler.signals.connect(
            self.spider_idle,
            signal=signals.spider_idle,
        )
        self.logger.info(
            "Reading dmoz start URLs from Redis key %s "
            "(batch_size=%d, max_idle_time=%d)",
            self.redis_key,
            self.redis_batch_size,
            self.max_idle_time,
        )
