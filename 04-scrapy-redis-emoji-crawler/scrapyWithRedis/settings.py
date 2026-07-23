# Scrapy settings for scrapyWithRedis project
#
# For simplicity, this file contains only settings considered important or
# commonly used. You can find more settings consulting the documentation:
#
#     https://docs.scrapy.org/en/latest/topics/settings.html
#     https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
#     https://docs.scrapy.org/en/latest/topics/spider-middleware.html

"""Project settings. Runtime-specific values come from environment variables."""

import os
from pathlib import Path

from dotenv import load_dotenv


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DOTENV_PATH = Path(
    os.getenv("SCRAPY_DOTENV_PATH", _PROJECT_ROOT / ".env")
)
load_dotenv(dotenv_path=_DOTENV_PATH, override=False)

BOT_NAME = "scrapyWithRedis"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SPIDER_MODULES = ["scrapyWithRedis.spiders"]
NEWSPIDER_MODULE = "scrapyWithRedis.spiders"

ADDONS = {}


# Crawl responsibly by identifying yourself (and your website) on the user-agent
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'

# Obey robots.txt rules
# ROBOTSTXT_OBEY = True

# Redis key namespace
REDIS_KEY_PREFIX = "scrapyWithRedis"
REDIS_START_URLS_KEY = f"{REDIS_KEY_PREFIX}:%(name)s:start_urls"
START_URL_DUPE_KEY = f"{REDIS_KEY_PREFIX}:%(spider)s:dupe:start_urls"
SCHEDULER_QUEUE_KEY = f"{REDIS_KEY_PREFIX}:%(spider)s:requests"
SCHEDULER_DUPEFILTER_KEY = f"{REDIS_KEY_PREFIX}:dupe:requests"
ITEM_URL_DUPE_KEY = f"{REDIS_KEY_PREFIX}:dupe:item_urls"
REDIS_ITEMS_KEY = f"{REDIS_KEY_PREFIX}:%(spider)s:items"
PAGE_FAILURE_QUEUE_KEY = (
    f"{REDIS_KEY_PREFIX}:%(spider)s:failures:pages"
)
PAGE_FAILURE_DUPE_KEY = (
    f"{REDIS_KEY_PREFIX}:%(spider)s:failures:pages:dupe"
)
IMAGE_FAILURE_QUEUE_KEY = (
    f"{REDIS_KEY_PREFIX}:%(spider)s:failures:images"
)
IMAGE_FAILURE_DUPE_KEY = f"{REDIS_KEY_PREFIX}:failures:images:dupe"
IMAGE_RETRY_QUEUE_KEY = f"{REDIS_KEY_PREFIX}:image_retry:start_urls"

# 指定去重方式 给请求对象去重
DUPEFILTER_CLASS = "scrapy_redis.dupefilter.RFPDupeFilter"
# 设置调度器
SCHEDULER = "scrapy_redis.scheduler.Scheduler"
# 队列中的内容是否进行持久保留
# True redis关闭的时候数据会保留
# False 不会保留
SCHEDULER_PERSIST = True

# Concurrency and throttling settings
CONCURRENT_REQUESTS = int(os.getenv("CONCURRENT_REQUESTS", "16"))
DMOZ_REDIS_BATCH_SIZE = int(
    os.getenv(
        "DMOZ_REDIS_BATCH_SIZE",
        str(min(CONCURRENT_REQUESTS, 50)),
    )
)
DMOZ_REDIS_MAX_IDLE_TIME = int(
    os.getenv("DMOZ_REDIS_MAX_IDLE_TIME", "30")
)
# CONCURRENT_REQUESTS_PER_DOMAIN = 1
DOWNLOAD_DELAY = float(os.getenv("DOWNLOAD_DELAY", "1"))

# Disable cookies (enabled by default)
#COOKIES_ENABLED = False

# Disable Telnet Console (enabled by default)
#TELNETCONSOLE_ENABLED = False

# Override the default request headers:
#DEFAULT_REQUEST_HEADERS = {
#    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
#    "Accept-Language": "en",
#}

# Enable or disable spider middlewares
# See https://docs.scrapy.org/en/latest/topics/spider-middleware.html
#SPIDER_MIDDLEWARES = {
#    "scrapyWithRedis.middlewares.ScrapywithredisSpiderMiddleware": 543,
#}

# Enable or disable downloader middlewares
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html
DOWNLOADER_MIDDLEWARES = {
   "scrapyWithRedis.middlewares.ScrapywithredisDownloaderMiddleware": 543,
}

# Enable or disable extensions
# See https://docs.scrapy.org/en/latest/topics/extensions.html
#EXTENSIONS = {
#    "scrapy.extensions.telnet.TelnetConsole": None,
#}

# Configure item pipelines
# See https://docs.scrapy.org/en/latest/topics/item-pipeline.html
ITEM_PIPELINES = {
    "scrapyWithRedis.pipelines.RequiredFieldsPipeline": 100,
    "scrapyWithRedis.pipelines.RedisItemUrlDedupPipeline": 200,
    "scrapyWithRedis.pipelines.EmojiImagesPipeline": 300,
    "scrapyWithRedis.pipelines.MetadataPipeline": 400,
    "scrapyWithRedis.pipelines.ImageFailurePipeline": 450,
    "scrapyWithRedis.pipelines.JsonLinesPipeline": 500,
    # 将完整结果保存到 Redis 中。
    "scrapy_redis.pipelines.RedisPipeline": 600,
}

OUTPUT_DIR = os.getenv("OUTPUT_DIR", "output")
IMAGES_STORE = os.getenv("IMAGES_STORE", "images")
WORKER_ID = os.getenv("WORKER_ID", "")

# Enable and configure the AutoThrottle extension (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/autothrottle.html
#AUTOTHROTTLE_ENABLED = True
# The initial download delay
#AUTOTHROTTLE_START_DELAY = 5
# The maximum download delay to be set in case of high latencies
#AUTOTHROTTLE_MAX_DELAY = 60
# The average number of requests Scrapy should be sending in parallel to
# each remote server
#AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0
# Enable showing throttling stats for every response received:
#AUTOTHROTTLE_DEBUG = False

# Enable and configure HTTP caching (disabled by default)
# See https://docs.scrapy.org/en/latest/topics/downloader-middleware.html#httpcache-middleware-settings
#HTTPCACHE_ENABLED = True
#HTTPCACHE_EXPIRATION_SECS = 0
#HTTPCACHE_DIR = "httpcache"
#HTTPCACHE_IGNORE_HTTP_CODES = []
#HTTPCACHE_STORAGE = "scrapy.extensions.httpcache.FilesystemCacheStorage"

# Set settings whose default value is deprecated to a future-proof value
FEED_EXPORT_ENCODING = "utf-8"
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
