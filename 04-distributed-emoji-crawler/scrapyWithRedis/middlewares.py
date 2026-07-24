# Define here the models for your spider middleware
#
# See documentation in:
# https://docs.scrapy.org/en/latest/topics/spider-middleware.html

from datetime import datetime, timezone

from scrapy import signals
from scrapy.utils.request import fingerprint
from scrapy_redis import connection

from .failure_store import RedisFailureStore
import logging
import random
logger = logging.getLogger(__name__)
# useful for handling different item types with a single interface
from itemadapter import ItemAdapter


class ScrapywithredisSpiderMiddleware:
    # Not all methods need to be defined. If a method is not defined,
    # scrapy acts as if the spider middleware does not modify the
    # passed objects.

    @classmethod
    def from_crawler(cls, crawler):
        # This method is used by Scrapy to create your spiders.
        s = cls()
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_spider_input(self, response, spider):
        # Called for each response that goes through the spider
        # middleware and into the spider.

        # Should return None or raise an exception.
        return None

    def process_spider_output(self, response, result, spider):
        # Called with the results returned from the Spider, after
        # it has processed the response.

        # Must return an iterable of Request, or item objects.
        for i in result:
            yield i

    def process_spider_exception(self, response, exception, spider):
        # Called when a spider or process_spider_input() method
        # (from other spider middleware) raises an exception.

        # Should return either None or an iterable of Request or item objects.
        pass

    async def process_start(self, start):
        # Called with an async iterator over the spider start() method or the
        # maching method of an earlier spider middleware.
        async for item_or_request in start:
            yield item_or_request

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s" % spider.name)


class ScrapywithredisDownloaderMiddleware:
    # Not all methods need to be defined. If a method is not defined,
    # scrapy acts as if the downloader middleware does not modify the
    # passed objects.
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
    ]

    def __init__(
        self,
        failure_store,
        page_queue_key,
        page_dedupe_key,
        worker_id,
        retry_times,
    ):
        self.failure_store = failure_store
        self.page_queue_key = page_queue_key
        self.page_dedupe_key = page_dedupe_key
        self.worker_id = worker_id
        self.retry_times = int(retry_times)

    @classmethod
    def from_crawler(cls, crawler):
        # This method is used by Scrapy to create your spiders.
        server = connection.from_settings(crawler.settings)
        s = cls(
            failure_store=RedisFailureStore(server),
            page_queue_key=crawler.settings.get(
                "PAGE_FAILURE_QUEUE_KEY",
                "scrapyWithRedis:%(spider)s:failures:pages",
            ),
            page_dedupe_key=crawler.settings.get(
                "PAGE_FAILURE_DUPE_KEY",
                "scrapyWithRedis:%(spider)s:failures:pages:dupe",
            ),
            worker_id=crawler.settings.get("WORKER_ID", ""),
            retry_times=crawler.settings.getint("RETRY_TIMES"),
        )
        crawler.signals.connect(s.spider_opened, signal=signals.spider_opened)
        return s

    def process_request(self, request, spider):
        # Called for each request that goes through the downloader
        # middleware.

        # Must either:
        # - return None: continue processing this request
        # - or return a Response object
        # - or return a Request object
        # - or raise IgnoreRequest: process_exception() methods of
        #   installed downloader middleware will be called

        request.headers["User-Agent"] = random.choice(self.USER_AGENTS)
        logger.info("request url=%s ua=%s", request.url, request.headers.get("User-Agent"))
        return None

    def process_response(self, request, response, spider):
        # Called with the response returned from the downloader.

        # Must either;
        # - return a Response object
        # - return a Request object
        # - or raise IgnoreRequest
        if response.status != 200:
            logger.warning("non-200 response status=%s url=%s", response.status, response.url)
            if not request.meta.get("is_image_request"):
                self._record_page_failure(
                    request,
                    spider,
                    status=response.status,
                )
        else:
            logger.info("200 response url=%s", response.url)
        return response

    def process_exception(self, request, exception, spider):
        # Called when a download handler or a process_request()
        # (from other downloader middleware) raises an exception.

        # Must either:
        # - return None: continue processing this exception
        # - return a Response object: stops process_exception() chain
        # - return a Request object: stops process_exception() chain
        # process_exception runs from lower to higher middleware priority,
        # so this middleware sees exceptions before RetryMiddleware does.
        # Only the final failed attempt belongs in the failure queue.
        if (
            not request.meta.get("is_image_request")
            and self._retries_exhausted(request)
        ):
            self._record_page_failure(
                request,
                spider,
                exception=exception,
            )
        return None

    def _retries_exhausted(self, request):
        retry_times = int(request.meta.get("retry_times", 0))
        max_retry_times = int(
            request.meta.get("max_retry_times", self.retry_times)
        )
        return retry_times >= max_retry_times

    def _record_page_failure(
        self,
        request,
        spider,
        status=None,
        exception=None,
    ):
        failure_id = fingerprint(request).hex()
        retry_times = int(request.meta.get("retry_times", 0))
        record = {
            "failure_id": failure_id,
            "url": request.url,
            "method": request.method,
            "status": status,
            "exception_type": (
                type(exception).__name__ if exception else ""
            ),
            "spider": spider.name,
            "worker_id": self.worker_id,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "retry_times": retry_times,
            "total_attempts": retry_times + 1,
        }
        try:
            self.failure_store.enqueue(
                queue_key=self.page_queue_key % {"spider": spider.name},
                dedupe_key=(
                    self.page_dedupe_key % {"spider": spider.name}
                ),
                failure_id=failure_id,
                record=record,
            )
        except Exception:
            spider.logger.error(
                "failed to store page request failure url=%s",
                request.url,
                exc_info=True,
            )

    def spider_opened(self, spider):
        spider.logger.info("Spider opened: %s" % spider.name)
