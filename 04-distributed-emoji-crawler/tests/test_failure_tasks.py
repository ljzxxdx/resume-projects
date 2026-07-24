import json
import unittest
from contextlib import redirect_stderr
from io import StringIO

from scrapyWithRedis.failure_store import RedisFailureStore
from tests.test_failure_store import InMemoryLuaRedis

try:
    import failure_tasks
except ModuleNotFoundError:
    find_failure = None
    build_parser = None
    replay_all_image_failures = None
    replay_all_page_failures = None
    replay_image_failure = None
    replay_page_failure = None
else:
    find_failure = failure_tasks.find_failure
    build_parser = failure_tasks.build_parser
    replay_all_image_failures = getattr(
        failure_tasks, "replay_all_image_failures", None
    )
    replay_all_page_failures = getattr(
        failure_tasks, "replay_all_page_failures", None
    )
    replay_image_failure = failure_tasks.replay_image_failure
    replay_page_failure = failure_tasks.replay_page_failure


class FailureTaskTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(find_failure, "failure_tasks.py must exist")
        self.assertIsNotNone(
            replay_all_page_failures,
            "batch page replay must exist",
        )
        self.assertIsNotNone(
            replay_all_image_failures,
            "batch image replay must exist",
        )
        self.server = InMemoryLuaRedis()
        self.store = RedisFailureStore(self.server)

    def test_replay_selector_requires_exactly_one_mode(self):
        parser = build_parser()

        parser.parse_args([
            "replay", "page", "--spider", "mycrawler_redis", "--all"
        ])
        parser.parse_args([
            "replay", "page", "--spider", "mycrawler_redis",
            "--failure-id", "page-1",
        ])

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args([
                "replay", "page", "--spider", "mycrawler_redis",
            ])
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args([
                "replay", "page", "--spider", "mycrawler_redis",
                "--all", "--failure-id", "page-1",
            ])

    def test_limit_must_be_positive(self):
        parser = build_parser()

        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            parser.parse_args([
                "replay", "image", "--spider", "mycrawler_redis",
                "--all", "--limit", "0",
            ])

    def test_find_failure_returns_record_and_exact_raw_payload(self):
        record = {"failure_id": "page-1", "url": "https://example.com/1"}
        self.store.enqueue("pages", "pages:dupe", "page-1", record)

        raw, actual = find_failure(self.store, "pages", "page-1")

        self.assertEqual(actual, record)
        self.assertEqual(raw, self.server.lists["pages"][0])

    def test_page_replay_enters_target_start_queue_only_once(self):
        record = {"failure_id": "page-1", "url": "https://example.com/1"}
        self.store.enqueue("pages", "pages:dupe", "page-1", record)

        first = replay_page_failure(
            self.store,
            queue_key="pages",
            dedupe_key="pages:dupe",
            retry_queue_key="mycrawler:start_urls",
            failure_id="page-1",
        )
        second = replay_page_failure(
            self.store,
            queue_key="pages",
            dedupe_key="pages:dupe",
            retry_queue_key="mycrawler:start_urls",
            failure_id="page-1",
        )

        self.assertTrue(first)
        self.assertFalse(second)
        payloads = self.server.lists["mycrawler:start_urls"]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(json.loads(payloads[0]), {"url": record["url"]})

    def test_image_replay_builds_retry_item_and_is_idempotent(self):
        img_url = "https://example.com/a.gif"
        record = {
            "failure_id": img_url,
            "img_url": img_url,
            "title": "demo",
            "source_url": "https://example.com/page/1",
        }
        self.server.sets["items:dupe"] = {img_url}
        self.store.enqueue("images", "images:dupe", img_url, record)

        first = replay_image_failure(
            self.store,
            queue_key="images",
            dedupe_key="images:dupe",
            item_dupe_key="items:dupe",
            retry_queue_key="image_retry:start_urls",
            failure_id=img_url,
        )
        second = replay_image_failure(
            self.store,
            queue_key="images",
            dedupe_key="images:dupe",
            item_dupe_key="items:dupe",
            retry_queue_key="image_retry:start_urls",
            failure_id=img_url,
        )

        self.assertTrue(first)
        self.assertFalse(second)
        payloads = self.server.lists["image_retry:start_urls"]
        self.assertEqual(len(payloads), 1)
        retry_data = json.loads(payloads[0])
        self.assertEqual(retry_data["url"], record["source_url"])
        self.assertEqual(retry_data["meta"]["retry_item"]["img_url"], img_url)
        self.assertNotIn(img_url, self.server.sets["items:dupe"])

    def test_replays_page_snapshot_with_one_queue_read(self):
        records = [
            {"failure_id": f"page-{index}", "url": f"https://example.com/{index}"}
            for index in range(3)
        ]
        for record in records:
            self.store.enqueue(
                "pages", "pages:dupe", record["failure_id"], record
            )
        read_count = 0
        original_list_raw = self.store.list_raw

        def counted_list_raw(queue_key, start=0, end=-1):
            nonlocal read_count
            read_count += 1
            return original_list_raw(queue_key, start, end)

        self.store.list_raw = counted_list_raw

        stats = replay_all_page_failures(
            self.store,
            queue_key="pages",
            dedupe_key="pages:dupe",
            retry_queue_key="mycrawler:start_urls",
        )

        self.assertEqual(read_count, 1)
        self.assertEqual(
            stats,
            {"scanned": 3, "queued": 3, "already_claimed": 0, "invalid": 0},
        )
        self.assertEqual(len(self.server.lists["mycrawler:start_urls"]), 3)
        self.assertEqual(self.server.lists["pages"], [])

    def test_image_batch_limit_only_processes_snapshot_prefix(self):
        for index in range(3):
            img_url = f"https://example.com/{index}.gif"
            record = {
                "failure_id": img_url,
                "img_url": img_url,
                "title": str(index),
                "source_url": f"https://example.com/page/{index}",
            }
            self.server.sets.setdefault("items:dupe", set()).add(img_url)
            self.store.enqueue("images", "images:dupe", img_url, record)

        lrange_calls = []
        original_lrange = self.server.lrange

        def recording_lrange(key, start, end):
            lrange_calls.append((key, start, end))
            return original_lrange(key, start, end)

        self.server.lrange = recording_lrange

        stats = replay_all_image_failures(
            self.store,
            queue_key="images",
            dedupe_key="images:dupe",
            item_dupe_key="items:dupe",
            retry_queue_key="image_retry:start_urls",
            limit=2,
        )

        self.assertEqual(stats["scanned"], 2)
        self.assertEqual(stats["queued"], 2)
        self.assertEqual(lrange_calls, [("images", 0, 1)])
        self.assertEqual(len(self.server.lists["images"]), 1)
        self.assertEqual(len(self.server.lists["image_retry:start_urls"]), 2)

    def test_batch_keeps_invalid_record_and_reports_it(self):
        self.server.lists["pages"] = ["not-json"]

        stats = replay_all_page_failures(
            self.store,
            queue_key="pages",
            dedupe_key="pages:dupe",
            retry_queue_key="mycrawler:start_urls",
        )

        self.assertEqual(
            stats,
            {"scanned": 1, "queued": 0, "already_claimed": 0, "invalid": 1},
        )
        self.assertEqual(self.server.lists["pages"], ["not-json"])


if __name__ == "__main__":
    unittest.main()
