import logging
import unittest
from datetime import datetime
from types import SimpleNamespace

from scrapy import Request
from scrapy.http import Response

from scrapyWithRedis.items import EmojiItem
from scrapyWithRedis.middlewares import ScrapywithredisDownloaderMiddleware
from scrapyWithRedis import pipelines


class RecordingFailureStore:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def enqueue(self, queue_key, dedupe_key, failure_id, record):
        if self.error:
            raise self.error
        self.calls.append(
            {
                "queue_key": queue_key,
                "dedupe_key": dedupe_key,
                "failure_id": failure_id,
                "record": record,
            }
        )
        return True


class PageFailureMiddlewareTests(unittest.TestCase):
    def make_middleware(self, store):
        return ScrapywithredisDownloaderMiddleware(
            failure_store=store,
            page_queue_key="scrapyWithRedis:%(spider)s:failures:pages",
            page_dedupe_key=(
                "scrapyWithRedis:%(spider)s:failures:pages:dupe"
            ),
            worker_id="worker-a",
            retry_times=2,
        )

    def make_spider(self):
        return SimpleNamespace(
            name="mycrawler_redis",
            logger=logging.getLogger("tests.page_failure"),
        )

    def test_records_final_http_failure_with_retry_metadata(self):
        store = RecordingFailureStore()
        middleware = self.make_middleware(store)
        request = Request(
            "https://example.com/page/1",
            meta={"retry_times": 2},
        )
        response = Response(request.url, status=500, request=request)

        result = middleware.process_response(
            request,
            response,
            self.make_spider(),
        )

        self.assertIs(result, response)
        self.assertEqual(len(store.calls), 1)
        call = store.calls[0]
        self.assertEqual(
            call["queue_key"],
            "scrapyWithRedis:mycrawler_redis:failures:pages",
        )
        record = call["record"]
        self.assertEqual(record["url"], request.url)
        self.assertEqual(record["status"], 500)
        self.assertEqual(record["exception_type"], "")
        self.assertEqual(record["spider"], "mycrawler_redis")
        self.assertEqual(record["worker_id"], "worker-a")
        self.assertEqual(record["retry_times"], 2)
        self.assertEqual(record["total_attempts"], 3)
        self.assertEqual(record["failure_id"], call["failure_id"])
        self.assertIsNotNone(datetime.fromisoformat(record["failed_at"]).tzinfo)

    def test_records_final_exception_type(self):
        store = RecordingFailureStore()
        middleware = self.make_middleware(store)
        request = Request(
            "https://example.com/page/2",
            meta={"retry_times": 2},
        )

        result = middleware.process_exception(
            request,
            TimeoutError("timed out"),
            self.make_spider(),
        )

        self.assertIsNone(result)
        record = store.calls[0]["record"]
        self.assertIsNone(record["status"])
        self.assertEqual(record["exception_type"], "TimeoutError")

    def test_does_not_record_exception_before_retries_are_exhausted(self):
        store = RecordingFailureStore()
        middleware = self.make_middleware(store)
        request = Request(
            "https://example.com/page/retry-me",
            meta={"retry_times": 1},
        )

        result = middleware.process_exception(
            request,
            TimeoutError("temporary timeout"),
            self.make_spider(),
        )

        self.assertIsNone(result)
        self.assertEqual(store.calls, [])

    def test_does_not_treat_image_request_as_page_failure(self):
        store = RecordingFailureStore()
        middleware = self.make_middleware(store)
        request = Request(
            "https://example.com/a.gif",
            meta={"is_image_request": True, "retry_times": 2},
        )
        response = Response(request.url, status=500, request=request)

        middleware.process_response(request, response, self.make_spider())
        middleware.process_exception(
            request,
            TimeoutError(),
            self.make_spider(),
        )

        self.assertEqual(store.calls, [])


class ImageFailurePipelineTests(unittest.TestCase):
    def make_item(self):
        return EmojiItem(
            img_url="https://example.com/a.gif",
            title="demo",
            source_url="https://example.com/page/1",
            image_path="",
            image_checksum="",
            download_status="failed",
            download_error="HTTP 404 Not Found",
            crawled="2026-07-19T01:00:00+00:00",
            spider="mycrawler_redis",
            worker_id="worker-a",
        )

    def make_pipeline(self, store):
        pipeline_class = getattr(pipelines, "ImageFailurePipeline", None)
        self.assertIsNotNone(
            pipeline_class,
            "ImageFailurePipeline must exist",
        )
        return pipeline_class(
            failure_store=store,
            queue_key="scrapyWithRedis:%(spider)s:failures:images",
            dedupe_key="scrapyWithRedis:failures:images:dupe",
        )

    def test_failed_image_is_enqueued_and_item_is_preserved(self):
        store = RecordingFailureStore()
        pipeline = self.make_pipeline(store)
        item = self.make_item()
        spider = SimpleNamespace(
            name="mycrawler_redis",
            logger=logging.getLogger("tests.image_failure"),
        )

        result = pipeline.process_item(item, spider)

        self.assertIs(result, item)
        self.assertEqual(len(store.calls), 1)
        record = store.calls[0]["record"]
        self.assertEqual(record["img_url"], item["img_url"])
        self.assertEqual(record["source_url"], item["source_url"])
        self.assertEqual(record["download_error"], item["download_error"])
        self.assertEqual(record["spider"], item["spider"])
        self.assertEqual(record["worker_id"], item["worker_id"])
        self.assertEqual(store.calls[0]["failure_id"], item["img_url"])
        self.assertIsNotNone(datetime.fromisoformat(record["failed_at"]).tzinfo)

    def test_successful_image_is_not_enqueued(self):
        store = RecordingFailureStore()
        pipeline = self.make_pipeline(store)
        item = self.make_item()
        item["download_status"] = "downloaded"

        result = pipeline.process_item(
            item,
            SimpleNamespace(name="mycrawler_redis"),
        )

        self.assertIs(result, item)
        self.assertEqual(store.calls, [])

    def test_redis_error_does_not_drop_failed_item(self):
        store = RecordingFailureStore(error=RuntimeError("redis unavailable"))
        pipeline = self.make_pipeline(store)
        item = self.make_item()
        spider = SimpleNamespace(
            name="mycrawler_redis",
            logger=logging.getLogger("tests.image_failure.redis_error"),
        )

        with self.assertLogs(spider.logger, level="ERROR"):
            result = pipeline.process_item(item, spider)

        self.assertIs(result, item)


if __name__ == "__main__":
    unittest.main()
