import unittest

from scrapy.http import HtmlResponse, Request

try:
    from scrapyWithRedis.spiders.image_retry import ImageRetrySpider
except ModuleNotFoundError:
    ImageRetrySpider = None


class ImageRetrySpiderTests(unittest.TestCase):
    def test_rebuilds_one_image_item_from_start_request_metadata(self):
        self.assertIsNotNone(ImageRetrySpider, "image_retry spider must exist")
        retry_item = {
            "img_url": "https://example.com/a.gif",
            "title": "demo",
            "source_url": "https://example.com/page/1",
        }
        request = Request(
            retry_item["source_url"],
            meta={"retry_item": retry_item},
        )
        response = HtmlResponse(
            request.url,
            request=request,
            body=b"<html></html>",
            encoding="utf-8",
        )

        items = list(ImageRetrySpider().parse(response))

        self.assertEqual(len(items), 1)
        self.assertEqual(dict(items[0]), retry_item)

