from scrapy_redis.spiders import RedisSpider

from ..items import EmojiItem


class ImageRetrySpider(RedisSpider):
    name = "image_retry"

    def parse(self, response):
        retry_item = response.meta.get("retry_item")
        if not retry_item:
            self.logger.warning(
                "image retry request missing retry_item url=%s",
                response.url,
            )
            return

        yield EmojiItem(
            img_url=retry_item.get("img_url", ""),
            title=retry_item.get("title", ""),
            source_url=retry_item.get("source_url", response.url),
        )
