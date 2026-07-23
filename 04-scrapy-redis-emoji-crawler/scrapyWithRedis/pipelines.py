# Define your item pipelines here
#
# Don't forget to add your pipeline to the ITEM_PIPELINES setting
# See: https://docs.scrapy.org/en/latest/topics/item-pipeline.html
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from itemadapter import ItemAdapter
from scrapy import Request
from scrapy.exceptions import DropItem
from scrapy.http.request import NO_CALLBACK
from scrapy.pipelines.files import FileException
from scrapy.pipelines.images import ImagesPipeline
from scrapy_redis import connection

from .failure_store import RedisFailureStore


logger = logging.getLogger(__name__)


class RequiredFieldsPipeline:
    required_fields = ("img_url", "source_url")

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        missing_fields = [
            field
            for field in self.required_fields
            if self._is_blank(adapter.get(field))
        ]
        if missing_fields:
            logger.warning(
                "event=item_dropped reason=missing_required_fields "
                "fields=%s img_url=%r source_url=%r spider=%s",
                ",".join(missing_fields),
                adapter.get("img_url", ""),
                adapter.get("source_url", ""),
                getattr(spider, "name", ""),
            )
            raise DropItem(
                f"Missing required fields: {', '.join(missing_fields)}"
            )
        return item

    @staticmethod
    def _is_blank(value):
        return value is None or (
            isinstance(value, str) and not value.strip()
        )


class RedisItemUrlDedupPipeline:
    def __init__(self, server, key):
        self.server = server
        self.key = key

    @classmethod
    def from_crawler(cls, crawler):
        return cls(
            server=connection.from_settings(crawler.settings),
            key=crawler.settings.get(
                "ITEM_URL_DUPE_KEY",
                "scrapyWithRedis:dupe:item_urls",
            ),
        )

    def process_item(self, item, spider):
        img_url = ItemAdapter(item)["img_url"]
        if not self.server.sadd(self.key, img_url):
            raise DropItem(f"Duplicate image URL: {img_url}")
        return item


class EmojiImagesPipeline(ImagesPipeline):
    def get_media_requests(self, item, info):
        img_url = ItemAdapter(item)["img_url"]
        return [Request(
            img_url,
            callback=NO_CALLBACK,
            meta={"is_image_request": True},
            headers={
                "Referer": ItemAdapter(item)["source_url"],
            },
        )]

    def item_completed(self, results, item, info):
        adapter = ItemAdapter(item)
        download_succeeded, result = results[0]

        if download_succeeded:
            adapter["image_path"] = result["path"]
            adapter["image_checksum"] = result["checksum"]
            adapter["download_status"] = result["status"]
            adapter["download_error"] = ""
            return item

        adapter["image_path"] = ""
        adapter["image_checksum"] = ""
        adapter["download_status"] = "failed"
        adapter["download_error"] = self._failure_summary(result)
        return item

    def media_failed(self, failure, request, info):
        original_summary = self._failure_summary(failure)
        try:
            return super().media_failed(failure, request, info)
        except FileException as error:
            if original_summary:
                raise FileException(original_summary) from error
            raise

    @staticmethod
    def _failure_summary(failure):
        if hasattr(failure, "getErrorMessage"):
            message = failure.getErrorMessage()
            if message:
                return message
        value = getattr(failure, "value", failure)
        message = str(value)
        return message or type(value).__name__


class MetadataPipeline:
    def __init__(self, worker_id=None):
        self.worker_id = worker_id

    @classmethod
    def from_crawler(cls, crawler):
        return cls(worker_id=crawler.settings.get("WORKER_ID", ""))

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        adapter["crawled"] = datetime.now(timezone.utc).isoformat()
        adapter["spider"] = spider.name
        adapter["worker_id"] = (
            self.worker_id
            if self.worker_id is not None
            else os.getenv("WORKER_ID", "")
        )
        return item


class ImageFailurePipeline:
    def __init__(self, failure_store, queue_key, dedupe_key):
        self.failure_store = failure_store
        self.queue_key = queue_key
        self.dedupe_key = dedupe_key

    @classmethod
    def from_crawler(cls, crawler):
        server = connection.from_settings(crawler.settings)
        return cls(
            failure_store=RedisFailureStore(server),
            queue_key=crawler.settings.get(
                "IMAGE_FAILURE_QUEUE_KEY",
                "scrapyWithRedis:%(spider)s:failures:images",
            ),
            dedupe_key=crawler.settings.get(
                "IMAGE_FAILURE_DUPE_KEY",
                "scrapyWithRedis:failures:images:dupe",
            ),
        )

    def process_item(self, item, spider):
        adapter = ItemAdapter(item)
        if adapter.get("download_status") != "failed":
            return item

        img_url = adapter.get("img_url", "")
        record = {
            "failure_id": img_url,
            "img_url": img_url,
            "title": adapter.get("title", ""),
            "source_url": adapter.get("source_url", ""),
            "download_error": adapter.get("download_error", ""),
            "spider": adapter.get("spider", spider.name),
            "worker_id": adapter.get("worker_id", ""),
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        queue_key = self.queue_key % {"spider": spider.name}
        try:
            self.failure_store.enqueue(
                queue_key=queue_key,
                dedupe_key=self.dedupe_key,
                failure_id=img_url,
                record=record,
            )
        except Exception:
            spider.logger.error(
                "failed to store image download failure img_url=%s",
                img_url,
                exc_info=True,
            )
        return item


class JsonLinesPipeline:
    def __init__(
        self,
        output_dir="output",
        worker_id="",
        process_id=None,
    ):
        self.output_dir = Path(output_dir)
        self.worker_id = self._safe_worker_id(worker_id)
        self.process_id = (
            os.getpid()
            if process_id is None
            else int(process_id)
        )
        self.output_path = self.output_dir / (
            f"items-{self.worker_id}-{self.process_id}.jl"
        )

    @classmethod
    def from_crawler(cls, crawler):
        return cls(
            output_dir=crawler.settings.get("OUTPUT_DIR", "output"),
            worker_id=crawler.settings.get("WORKER_ID", ""),
        )

    @staticmethod
    def _safe_worker_id(worker_id):
        value = str(worker_id or "worker").strip()
        safe_value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
        return safe_value.strip("._-") or "worker"

    def open_spider(self, spider):
        self.output_count = 0
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.output_path.open(
            "a",
            encoding="utf-8",
        )

    def process_item(self, item, spider):
        self.file.write(json.dumps(dict(item), ensure_ascii=False) + "\n")
        self.output_count += 1
        return item

    def close_spider(self, spider):
        self.file.close()
        logger.info(
            "json lines pipeline stats: output=%s path=%s",
            self.output_count,
            self.output_path,
        )
