import json
import os
import subprocess
import sys
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from scrapy.exceptions import DropItem
from scrapy.settings import Settings

from scrapyWithRedis import pipelines
from scrapyWithRedis import settings as project_settings
from scrapyWithRedis.items import EmojiItem


class InMemoryRedisSet:
    def __init__(self):
        self.members_by_key = {}

    def sadd(self, key, value):
        members = self.members_by_key.setdefault(key, set())
        if value in members:
            return 0
        members.add(value)
        return 1


class PipelineFlowTests(unittest.TestCase):
    def pipeline_class(self, name):
        pipeline_class = getattr(pipelines, name, None)
        self.assertIsNotNone(pipeline_class, f"{name} must be defined")
        return pipeline_class

    def make_item(self, **overrides):
        values = {
            "img_url": "https://example.com/emoji.gif",
            "source_url": "https://example.com/page/1",
            "title": "demo",
        }
        values.update(overrides)
        return EmojiItem(values)

    def test_required_fields_pipeline_rejects_missing_url_fields(self):
        pipeline = self.pipeline_class("RequiredFieldsPipeline")()

        for missing_field in ("img_url", "source_url"):
            with self.subTest(field=missing_field):
                item = self.make_item()
                del item[missing_field]
                with self.assertRaises(DropItem):
                    pipeline.process_item(item, spider=None)

    def test_required_fields_pipeline_keeps_item_without_title(self):
        pipeline = self.pipeline_class("RequiredFieldsPipeline")()
        item = self.make_item()
        del item["title"]

        processed_item = pipeline.process_item(item, spider=None)

        self.assertIs(processed_item, item)

    def test_required_fields_pipeline_rejects_blank_url_fields(self):
        pipeline = self.pipeline_class("RequiredFieldsPipeline")()

        for blank_field in ("img_url", "source_url"):
            with self.subTest(field=blank_field):
                item = self.make_item(**{blank_field: "   "})
                with self.assertLogs(
                    "scrapyWithRedis.pipelines",
                    level="WARNING",
                ) as captured, self.assertRaises(DropItem):
                    pipeline.process_item(
                        item,
                        spider=SimpleNamespace(name="test_spider"),
                    )

                log_output = " ".join(captured.output)
                self.assertIn("event=item_dropped", log_output)
                self.assertIn("reason=missing_required_fields", log_output)
                self.assertIn(f"fields={blank_field}", log_output)
                self.assertIn("img_url=", log_output)
                self.assertIn("source_url=", log_output)

    def test_redis_item_url_dedup_uses_one_atomic_set(self):
        server = InMemoryRedisSet()
        pipeline_class = self.pipeline_class("RedisItemUrlDedupPipeline")
        pipeline = pipeline_class(server, "emoji:item_urls")
        first_item = self.make_item()
        duplicate_item = self.make_item(title="duplicate")

        self.assertIs(pipeline.process_item(first_item, spider=None), first_item)
        with self.assertRaises(DropItem):
            pipeline.process_item(duplicate_item, spider=None)

        self.assertEqual(
            server.members_by_key["emoji:item_urls"],
            {first_item["img_url"]},
        )

    def test_redis_item_url_dedup_reads_crawler_settings(self):
        pipeline_class = self.pipeline_class("RedisItemUrlDedupPipeline")
        crawler = SimpleNamespace(
            settings=Settings(
                {
                    "REDIS_URL": "redis://127.0.0.1:6379/15",
                    "ITEM_URL_DUPE_KEY": "test:item_urls",
                }
            )
        )

        pipeline = pipeline_class.from_crawler(crawler)

        self.assertEqual(pipeline.key, "test:item_urls")
        self.assertTrue(hasattr(pipeline.server, "sadd"))

    def test_metadata_pipeline_adds_timezone_spider_and_worker(self):
        pipeline = self.pipeline_class("MetadataPipeline")()
        item = self.make_item(
            image_path="",
            image_checksum="",
            download_status="failed",
            download_error="HTTP 404 Not Found",
        )
        spider = SimpleNamespace(name="dmoz")

        with patch.dict(os.environ, {"WORKER_ID": "worker-a"}):
            processed_item = pipeline.process_item(item, spider)

        crawled = datetime.fromisoformat(item["crawled"])
        self.assertIs(processed_item, item)
        self.assertIsNotNone(crawled.tzinfo)
        self.assertEqual(item["spider"], "dmoz")
        self.assertEqual(item["worker_id"], "worker-a")
        self.assertEqual(item["download_status"], "failed")
        self.assertEqual(item["download_error"], "HTTP 404 Not Found")

    def test_json_lines_output_keeps_completed_failed_item(self):
        item = self.make_item(
            image_path="",
            image_checksum="",
            download_status="failed",
            download_error="HTTP 404 Not Found",
            crawled="2026-07-16T00:00:00+00:00",
            spider="dmoz",
            worker_id="worker-a",
        )

        original_directory = Path.cwd()
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory) / "results"
            pipeline = self.pipeline_class("JsonLinesPipeline")(
                output_dir=output_dir,
                worker_id="worker-a",
                process_id=1234,
            )
            try:
                os.chdir(temporary_directory)
                pipeline.open_spider(spider=None)
                processed_item = pipeline.process_item(item, spider=None)
                pipeline.close_spider(spider=None)
                output = json.loads(
                    (output_dir / "items-worker-a-1234.jl").read_text(
                        encoding="utf-8"
                    )
                )
            finally:
                os.chdir(original_directory)

        self.assertIs(processed_item, item)
        self.assertEqual(output, dict(item))
        self.assertEqual(output["download_status"], "failed")
        self.assertEqual(output["worker_id"], "worker-a")

    def test_images_store_can_be_overridden_by_environment(self):
        environment = os.environ.copy()
        environment["IMAGES_STORE"] = "custom-images"
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "from scrapyWithRedis.settings import IMAGES_STORE; print(IMAGES_STORE)",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "custom-images")

    def test_pipeline_order_prevents_exporting_partial_items(self):
        self.assertEqual(
            project_settings.ITEM_PIPELINES,
            {
                "scrapyWithRedis.pipelines.RequiredFieldsPipeline": 100,
                "scrapyWithRedis.pipelines.RedisItemUrlDedupPipeline": 200,
                "scrapyWithRedis.pipelines.EmojiImagesPipeline": 300,
                "scrapyWithRedis.pipelines.MetadataPipeline": 400,
                "scrapyWithRedis.pipelines.ImageFailurePipeline": 450,
                "scrapyWithRedis.pipelines.JsonLinesPipeline": 500,
                "scrapy_redis.pipelines.RedisPipeline": 600,
            },
        )


if __name__ == "__main__":
    unittest.main()
