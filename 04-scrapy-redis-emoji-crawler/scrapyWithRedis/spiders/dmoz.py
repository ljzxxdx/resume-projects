import json

from scrapy.linkextractors import LinkExtractor
from scrapy.spiders import CrawlSpider, Rule

from ..parsers import extract_emoji_items
from ..redis_start_queue import RedisStartQueueConsumer
from ..start_urls import normalize_start_urls


class DmozSpider(CrawlSpider):
    name = "dmoz"
    # allowed_domains = ["example.com"]
    start_urls = ["https://fabiaoqing.com/biaoqing"]

    rules = (
        Rule(LinkExtractor(
            # restrict_css=('.item',)
            restrict_xpaths=(
                '//div[@class="ui pagination menu"]'
                '/a[contains(@href,"page")]'
            )
        ), callback="parse_emoji_image", follow=True),
    )

    def __init__(
        self,
        start_url=None,
        start_urls_json=None,
        redis_start=False,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.redis_start = _as_bool(redis_start)
        self.redis_start_queue = None

        has_direct_urls = start_url is not None or start_urls_json is not None
        if self.redis_start and has_direct_urls:
            raise ValueError(
                "Redis start mode cannot be combined with direct start URLs"
            )
        if start_url is not None and start_urls_json is not None:
            raise ValueError(
                "start_url and start_urls_json cannot be used together"
            )

        if self.redis_start:
            self.start_urls = []
        elif start_urls_json is not None:
            try:
                values = json.loads(start_urls_json)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "start_urls_json must be a JSON array of URLs"
                ) from exc
            if not isinstance(values, list):
                raise ValueError(
                    "start_urls_json must be a JSON array of URLs"
                )
            self.start_urls = normalize_start_urls(values)
        elif start_url is not None:
            self.start_urls = normalize_start_urls([start_url])

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        if spider.redis_start:
            spider.redis_start_queue = RedisStartQueueConsumer(
                crawler,
                spider,
            )
        return spider

    async def start(self):
        if self.redis_start:
            if self.redis_start_queue is None:
                raise RuntimeError("Redis start queue is not initialized")
            for request in self.redis_start_queue.next_requests():
                yield request
            return

        async for request in super().start():
            yield request

    def parse_emoji_image(self, response):
        yield from extract_emoji_items(response, self.logger)

    def parse_start_url(self, response, **kwargs):
        yield from extract_emoji_items(response, self.logger)


def _as_bool(value):
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"invalid boolean value for redis_start: {value!r}")
