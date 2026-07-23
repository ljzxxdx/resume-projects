from scrapy.linkextractors import LinkExtractor
from scrapy.spiders import Rule
from scrapy_redis.spiders import RedisCrawlSpider

from ..parsers import extract_emoji_items


class MycrawlerRedisSpider(RedisCrawlSpider):
    name = "mycrawler_redis"

    rules = (
        Rule(LinkExtractor(
            # allow=r"Items/",
            restrict_xpaths=(
                '//div[@class="ui pagination menu"]'
                '/a[contains(@href,"page")]'
            )
        ), callback="parse_emoji_image", follow=True),
    )

    def __init__(self, *args, **kwargs):
        # Dynamically define the allowed domains list.
        domain = kwargs.pop('domain', '')
        self.allowed_domains = [
            value.strip()
            for value in domain.split(',')
            if value.strip()
        ]
        super(MycrawlerRedisSpider, self).__init__(*args, **kwargs)

    def parse_emoji_image(self, response):
        yield from extract_emoji_items(response, self.logger)

    def parse_start_url(self, response, **kwargs):
        yield from extract_emoji_items(response, self.logger)

