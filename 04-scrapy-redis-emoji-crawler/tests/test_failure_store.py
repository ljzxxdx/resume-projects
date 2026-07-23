import json
import unittest

try:
    from scrapyWithRedis.failure_store import RedisFailureStore
except ModuleNotFoundError:
    RedisFailureStore = None


class InMemoryLuaRedis:
    def __init__(self):
        self.lists = {}
        self.sets = {}

    def eval(self, script, numkeys, *values):
        keys = values[:numkeys]
        args = values[numkeys:]
        if numkeys == 2:
            queue_key, dedupe_key = keys
            failure_id, payload = args
            dedupe = self.sets.setdefault(dedupe_key, set())
            if failure_id in dedupe:
                return 0
            dedupe.add(failure_id)
            self.lists.setdefault(queue_key, []).append(payload)
            return 1

        if numkeys == 3:
            queue_key, dedupe_key, retry_queue_key = keys
            failure_id, payload, retry_payload = args
            queue = self.lists.setdefault(queue_key, [])
            if payload not in queue:
                return 0
            queue.remove(payload)
            self.sets.setdefault(dedupe_key, set()).discard(failure_id)
            self.lists.setdefault(retry_queue_key, []).append(retry_payload)
            return 1

        if numkeys == 4:
            queue_key, dedupe_key, item_dupe_key, retry_queue_key = keys
            failure_id, payload, img_url, retry_payload = args
            queue = self.lists.setdefault(queue_key, [])
            if payload not in queue:
                return 0
            queue.remove(payload)
            self.sets.setdefault(dedupe_key, set()).discard(failure_id)
            self.sets.setdefault(item_dupe_key, set()).discard(img_url)
            self.lists.setdefault(retry_queue_key, []).append(retry_payload)
            return 1

        raise AssertionError(f"unexpected numkeys: {numkeys}")

    def lrange(self, key, start, end):
        values = self.lists.get(key, [])
        if end == -1:
            return values[start:]
        return values[start:end + 1]


class RedisFailureStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(
            RedisFailureStore,
            "RedisFailureStore must exist",
        )
        self.server = InMemoryLuaRedis()
        self.store = RedisFailureStore(self.server)

    def test_enqueue_is_atomic_and_deduplicated(self):
        record = {"failure_id": "page-1", "url": "https://example.com"}

        first = self.store.enqueue(
            queue_key="failures:pages",
            dedupe_key="failures:pages:dupe",
            failure_id="page-1",
            record=record,
        )
        second = self.store.enqueue(
            queue_key="failures:pages",
            dedupe_key="failures:pages:dupe",
            failure_id="page-1",
            record=record,
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(self.server.lists["failures:pages"]), 1)
        self.assertEqual(
            self.store.list_records("failures:pages"),
            [record],
        )

    def test_page_replay_can_only_claim_one_failure_record_once(self):
        record = {"failure_id": "page-1", "url": "https://example.com"}
        self.store.enqueue(
            "failures:pages",
            "failures:pages:dupe",
            "page-1",
            record,
        )
        raw_payload = self.server.lists["failures:pages"][0]
        retry_payload = json.dumps({"url": record["url"]})

        first = self.store.replay_page(
            queue_key="failures:pages",
            dedupe_key="failures:pages:dupe",
            failure_id="page-1",
            failure_payload=raw_payload,
            retry_queue_key="retry:pages",
            retry_payload=retry_payload,
        )
        second = self.store.replay_page(
            queue_key="failures:pages",
            dedupe_key="failures:pages:dupe",
            failure_id="page-1",
            failure_payload=raw_payload,
            retry_queue_key="retry:pages",
            retry_payload=retry_payload,
        )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(self.server.lists["failures:pages"], [])
        self.assertEqual(self.server.lists["retry:pages"], [retry_payload])

    def test_image_replay_also_releases_item_url_deduplication(self):
        img_url = "https://example.com/a.gif"
        record = {"failure_id": img_url, "img_url": img_url}
        self.server.sets["items:dupe"] = {img_url}
        self.store.enqueue(
            "failures:images",
            "failures:images:dupe",
            img_url,
            record,
        )
        raw_payload = self.server.lists["failures:images"][0]

        replayed = self.store.replay_image(
            queue_key="failures:images",
            dedupe_key="failures:images:dupe",
            failure_id=img_url,
            failure_payload=raw_payload,
            item_dupe_key="items:dupe",
            img_url=img_url,
            retry_queue_key="retry:images",
            retry_payload=json.dumps({"url": "https://example.com/page"}),
        )

        self.assertTrue(replayed)
        self.assertNotIn(img_url, self.server.sets["items:dupe"])
        self.assertEqual(len(self.server.lists["retry:images"]), 1)


if __name__ == "__main__":
    unittest.main()
