import unittest
from types import SimpleNamespace

from scrapy import Request
from scrapy.http.request import NO_CALLBACK
from scrapy.pipelines.files import FileException
from scrapy.pipelines.images import ImagesPipeline
from twisted.python.failure import Failure

from scrapyWithRedis import pipelines
from scrapyWithRedis.items import EmojiItem


class EmojiImagesPipelineTests(unittest.TestCase):
    def make_pipeline(self):
        pipeline_class = getattr(pipelines, "EmojiImagesPipeline", None)
        self.assertIsNotNone(
            pipeline_class,
            "EmojiImagesPipeline must be defined",
        )
        return pipeline_class.__new__(pipeline_class)

    def make_item(self):
        return EmojiItem(
            img_url="https://example.com/emoji.gif",
            title="demo",
            source_url="https://example.com/page/1",
        )

    def test_inherits_scrapy_images_pipeline(self):
        pipeline = self.make_pipeline()

        self.assertIsInstance(pipeline, ImagesPipeline)

    def test_builds_one_request_from_single_img_url(self):
        pipeline = self.make_pipeline()
        item = self.make_item()

        requests = pipeline.get_media_requests(item, info=None)

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url, item["img_url"])
        self.assertIs(requests[0].callback, NO_CALLBACK)
        self.assertTrue(requests[0].meta["is_image_request"])
        self.assertNotIn("image_urls", item)
        self.assertNotIn("images", item)

    def test_maps_success_result_to_current_item(self):
        pipeline = self.make_pipeline()
        item = self.make_item()
        results = [
            (
                True,
                {
                    "url": item["img_url"],
                    "path": "full/emoji.gif",
                    "checksum": "abc123",
                    "status": "downloaded",
                },
            )
        ]

        completed_item = pipeline.item_completed(results, item, info=None)

        self.assertIs(completed_item, item)
        self.assertEqual(item["image_path"], "full/emoji.gif")
        self.assertEqual(item["image_checksum"], "abc123")
        self.assertEqual(item["download_status"], "downloaded")
        self.assertEqual(item["download_error"], "")

    def test_keeps_failed_item_and_records_error(self):
        pipeline = self.make_pipeline()
        item = self.make_item()
        results = [(False, Failure(RuntimeError("HTTP 404 Not Found")))]

        completed_item = pipeline.item_completed(results, item, info=None)

        self.assertIs(completed_item, item)
        self.assertEqual(item["image_path"], "")
        self.assertEqual(item["image_checksum"], "")
        self.assertEqual(item["download_status"], "failed")
        self.assertIn("HTTP 404 Not Found", item["download_error"])

    def test_media_failed_preserves_original_download_error(self):
        pipeline = self.make_pipeline()
        request = Request(
            "http://127.0.0.1:1/controlled-failure.jpg",
            headers={"Referer": "https://example.com/page/1"},
        )
        info = SimpleNamespace(
            spider=SimpleNamespace(name="image_retry"),
        )
        original_failure = Failure(
            ConnectionRefusedError("Connection refused")
        )

        with self.assertRaises(FileException) as captured:
            pipeline.media_failed(original_failure, request, info)

        self.assertIn("Connection refused", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
