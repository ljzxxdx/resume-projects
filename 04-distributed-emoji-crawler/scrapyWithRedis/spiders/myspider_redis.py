from scrapy_redis.spiders import RedisSpider

from ..parsers import extract_emoji_items


# 普通的框架改写的分布式爬虫
class MySpider(RedisSpider):
    """Spider that reads start URLs from the configured Redis queue."""
    name = 'myspider_redis'

    def __init__(self, *args, **kwargs):
        # Dynamically define the allowed domains list.
        # 允许爬取的域名范围
        domain = kwargs.pop('domain', '')
        self.allowed_domains = [
            value.strip()
            for value in domain.split(',')
            if value.strip()
        ]
        super(MySpider, self).__init__(*args, **kwargs)

    def parse(self, response):
        first_page_url = response.xpath('//div[@class="ui pagination menu"]/a[contains(@href,"page")][1]/@href').get(default="")
        if not first_page_url:
            self.logger.warning(
                "myspider_redis failed: missing first page url on '%s'",
                response.url,
            )
            raise Exception("missing first page url")
        yield response.follow(
            first_page_url,
            callback=self.parse_emoji_image,
        )

    def parse_emoji_image(self, response):
        yield from extract_emoji_items(response, self.logger)

        next_page_url = response.xpath('//div[@class="ui pagination menu"]/a[contains(text(), "下一页")]/@href').get(default="")
        if next_page_url:
            yield response.follow(
                next_page_url,
                callback=self.parse_emoji_image,
            )
