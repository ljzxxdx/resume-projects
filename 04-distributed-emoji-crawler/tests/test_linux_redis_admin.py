import unittest
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace

try:
    from scripts.linux.redis_admin import (
        collect_redis_observation,
        build_parser,
        enqueue_urls,
        normalize_urls,
        redis_key_size,
    )
except ModuleNotFoundError:
    collect_redis_observation = None
    build_parser = None
    enqueue_urls = None
    normalize_urls = None
    redis_key_size = None


class FakeRedis:
    def __init__(self):
        self.lists = {}
        self.sets = {}
        self.zsets = {}

    def eval(self, script, numkeys, *values):
        queue_key, dedupe_key = values[:numkeys]
        arguments = values[numkeys:]
        added = 0
        for index in range(0, len(arguments), 2):
            url, payload = arguments[index:index + 2]
            dedupe = self.sets.setdefault(dedupe_key, set())
            if url in dedupe:
                continue
            dedupe.add(url)
            self.lists.setdefault(queue_key, []).append(payload)
            added += 1
        return added

    def type(self, key):
        if key in self.lists:
            return b"list"
        if key in self.sets:
            return b"set"
        if key in self.zsets:
            return b"zset"
        return b"none"

    def llen(self, key):
        return len(self.lists.get(key, []))

    def scard(self, key):
        return len(self.sets.get(key, set()))

    def zcard(self, key):
        return len(self.zsets.get(key, {}))


class LinuxRedisAdminTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(enqueue_urls, "Linux Redis admin tool must exist")

    def test_normalizes_and_deduplicates_urls_in_input_order(self):
        urls = normalize_urls(
            [
                " https://example.com/page/2?b=2&a=1#fragment ",
                "https://example.com/page/2?a=1&b=2",
                "https://example.com/page/3",
            ]
        )

        self.assertEqual(
            urls,
            [
                "https://example.com/page/2?a=1&b=2",
                "https://example.com/page/3",
            ],
        )

    def test_rejects_non_http_urls(self):
        with self.assertRaisesRegex(ValueError, "http"):
            normalize_urls(["file:///tmp/page.html"])

    def test_enqueue_accepts_only_page_start_queue_spiders(self):
        parser = build_parser()
        for spider in ("dmoz", "myspider_redis", "mycrawler_redis"):
            with self.subTest(spider=spider):
                arguments = parser.parse_args(
                    [
                        "enqueue",
                        "--spider",
                        spider,
                        "https://example.com/page/1",
                    ]
                )
                self.assertEqual(arguments.spider, spider)

        with redirect_stderr(StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "enqueue",
                        "--spider",
                        "image_retry",
                        "https://example.com/page/1",
                    ]
                )

    def test_atomically_enqueues_only_new_urls_and_reports_lengths(self):
        server = FakeRedis()
        urls = [
            "https://example.com/page/1",
            "https://example.com/page/2",
            "https://example.com/page/1",
        ]

        first = enqueue_urls(
            server,
            spider="mycrawler_redis",
            urls=urls,
            start_key_template="project:%(name)s:start_urls",
            dedupe_key_template="project:%(spider)s:dupe:start_urls",
        )
        second = enqueue_urls(
            server,
            spider="mycrawler_redis",
            urls=urls,
            start_key_template="project:%(name)s:start_urls",
            dedupe_key_template="project:%(spider)s:dupe:start_urls",
        )

        self.assertEqual(first["before"], 0)
        self.assertEqual(first["after"], 2)
        self.assertEqual(first["added"], 2)
        self.assertEqual(first["duplicates"], 1)
        self.assertEqual(second["added"], 0)
        self.assertEqual(second["duplicates"], 3)
        self.assertEqual(
            len(server.lists["project:mycrawler_redis:start_urls"]),
            2,
        )

    def test_reports_size_using_the_real_redis_key_type(self):
        server = FakeRedis()
        server.lists["queue"] = ["a", "b"]
        server.sets["dupe"] = {"a"}
        server.zsets["scheduler"] = {"request": 0}

        self.assertEqual(redis_key_size(server, "queue"), ("list", 2))
        self.assertEqual(redis_key_size(server, "dupe"), ("set", 1))
        self.assertEqual(
            redis_key_size(server, "scheduler"),
            ("zset", 1),
        )
        self.assertEqual(redis_key_size(server, "missing"), ("none", 0))

    def test_collects_all_required_operational_keys(self):
        server = FakeRedis()
        settings = SimpleNamespace(
            REDIS_START_URLS_KEY="project:%(name)s:start_urls",
            START_URL_DUPE_KEY="project:%(spider)s:dupe:start_urls",
            SCHEDULER_QUEUE_KEY="project:%(spider)s:requests",
            SCHEDULER_DUPEFILTER_KEY="project:dupe:requests",
            ITEM_URL_DUPE_KEY="project:dupe:item_urls",
            REDIS_ITEMS_KEY="project:%(spider)s:items",
            PAGE_FAILURE_QUEUE_KEY="project:%(spider)s:failures:pages",
            IMAGE_FAILURE_QUEUE_KEY="project:%(spider)s:failures:images",
        )
        server.zsets["project:mycrawler_redis:requests"] = {"r": 0}
        server.sets["project:dupe:requests"] = {"fp"}

        records = collect_redis_observation(
            server,
            spider="mycrawler_redis",
            settings_module=settings,
        )

        self.assertEqual(
            [record["role"] for record in records],
            [
                "start_queue",
                "start_url_dupe",
                "scheduler_queue",
                "request_dupe",
                "item_url_dupe",
                "results",
                "page_failures",
                "image_failures",
            ],
        )
        scheduler = next(
            record for record in records
            if record["role"] == "scheduler_queue"
        )
        self.assertEqual(scheduler["type"], "zset")
        self.assertEqual(scheduler["size"], 1)


if __name__ == "__main__":
    unittest.main()
