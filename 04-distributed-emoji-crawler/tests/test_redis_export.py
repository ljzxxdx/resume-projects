import importlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def load_export_module():
    try:
        return importlib.import_module("export_redis_results")
    except ModuleNotFoundError:
        return None


class FakeRedisLists:
    def __init__(self, values_by_key):
        self.values_by_key = values_by_key
        self.lrange_calls = []

    def llen(self, key):
        return len(self.values_by_key.get(key, []))

    def lrange(self, key, start, end):
        self.lrange_calls.append((key, start, end))
        return self.values_by_key.get(key, [])[start:end + 1]


class RedisResultExportTests(unittest.TestCase):
    def test_exports_all_spiders_and_prefers_successful_image_result(self):
        export_module = load_export_module()
        self.assertIsNotNone(
            export_module,
            "export_redis_results module must exist",
        )
        key_template = "scrapyWithRedis:%(spider)s:items"
        server = FakeRedisLists(
            {
                "scrapyWithRedis:dmoz:items": [
                    json.dumps(
                        {
                            "img_url": "https://example.com/a.gif",
                            "download_status": "failed",
                            "crawled": "2026-07-19T01:00:00+00:00",
                        }
                    ).encode(),
                ],
                "scrapyWithRedis:mycrawler_redis:items": [
                    json.dumps(
                        {
                            "img_url": "https://example.com/a.gif",
                            "download_status": "downloaded",
                            "crawled": "2026-07-19T01:01:00+00:00",
                        }
                    ).encode(),
                    json.dumps(
                        {
                            "img_url": "https://example.com/b.gif",
                            "download_status": "downloaded",
                            "crawled": "2026-07-19T01:02:00+00:00",
                        }
                    ).encode(),
                ],
            }
        )

        with TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "all-items.jl"
            stats = export_module.export_redis_results(
                server=server,
                key_template=key_template,
                spider_names=("dmoz", "mycrawler_redis"),
                output_file=output_path,
                batch_size=1,
            )
            first_content = output_path.read_text(encoding="utf-8")
            second_stats = export_module.export_redis_results(
                server=server,
                key_template=key_template,
                spider_names=("dmoz", "mycrawler_redis"),
                output_file=output_path,
                batch_size=1,
            )
            second_content = output_path.read_text(encoding="utf-8")

        items = [json.loads(line) for line in first_content.splitlines()]
        self.assertEqual(
            [item["img_url"] for item in items],
            [
                "https://example.com/a.gif",
                "https://example.com/b.gif",
            ],
        )
        self.assertEqual(items[0]["download_status"], "downloaded")
        self.assertEqual(
            stats,
            {"read": 3, "exported": 2, "duplicates": 1},
        )
        self.assertEqual(second_stats, stats)
        self.assertEqual(second_content, first_content)
        self.assertTrue(
            all(end - start + 1 <= 1 for _, start, end in server.lrange_calls)
        )


if __name__ == "__main__":
    unittest.main()
