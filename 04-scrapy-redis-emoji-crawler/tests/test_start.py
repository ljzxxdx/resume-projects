import importlib
import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch


def load_start_module():
    with patch("scrapy.cmdline.execute"):
        return importlib.import_module("start")


class StartCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.start = load_start_module()

    def build_command(self, argv=None, environ=None):
        build_command = getattr(self.start, "build_command", None)
        self.assertIsNotNone(build_command, "build_command must be defined")
        return build_command(argv or [], environ or {})

    def test_defaults_to_dmoz(self):
        self.assertEqual(
            self.build_command(),
            ["scrapy", "crawl", "dmoz"],
        )

    def test_selects_spider_from_environment(self):
        self.assertEqual(
            self.build_command(
                environ={"SPIDER_NAME": "myspider_redis"},
            ),
            ["scrapy", "crawl", "myspider_redis"],
        )

    def test_command_line_spider_overrides_environment(self):
        self.assertEqual(
            self.build_command(
                ["--spider", "mycrawler_redis"],
                {"SPIDER_NAME": "myspider_redis"},
            ),
            ["scrapy", "crawl", "mycrawler_redis"],
        )

    def test_forwards_remaining_arguments_to_scrapy(self):
        self.assertEqual(
            self.build_command(
                ["--spider", "dmoz", "-o", "output.json"],
            ),
            ["scrapy", "crawl", "dmoz", "-o", "output.json"],
        )

    def test_collects_normalizes_and_deduplicates_dmoz_start_urls(self):
        command = self.build_command(
            [
                "--spider",
                "dmoz",
                "--start-url",
                "https://example.com/page/2?b=2&a=1#fragment",
                "--start-url",
                "https://example.com/page/2?a=1&b=2",
                "--start-url",
                "https://example.com/page/3",
            ]
        )

        self.assertEqual(command[:3], ["scrapy", "crawl", "dmoz"])
        self.assertEqual(command[3], "-a")
        name, payload = command[4].split("=", 1)
        self.assertEqual(name, "start_urls_json")
        self.assertEqual(
            json.loads(payload),
            [
                "https://example.com/page/2?a=1&b=2",
                "https://example.com/page/3",
            ],
        )

    def test_rejects_start_url_for_non_dmoz_spider(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as captured:
                self.build_command(
                    [
                        "--spider",
                        "mycrawler_redis",
                        "--start-url",
                        "https://example.com/page/1",
                    ]
                )

        self.assertEqual(captured.exception.code, 2)

    def test_rejects_invalid_start_url(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as captured:
                self.build_command(
                    ["--spider", "dmoz", "--start-url", "file:///tmp/a"],
                )

        self.assertEqual(captured.exception.code, 2)

    def test_rejects_direct_urls_combined_with_redis_mode(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as captured:
                self.build_command(
                    [
                        "--spider",
                        "dmoz",
                        "--start-url",
                        "https://example.com/page/1",
                        "-a",
                        "redis_start=true",
                    ]
                )

        self.assertEqual(captured.exception.code, 2)

    def test_can_select_image_retry_spider(self):
        self.assertEqual(
            self.build_command(["--spider", "image_retry"]),
            ["scrapy", "crawl", "image_retry"],
        )

    def test_rejects_unknown_environment_spider(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as captured:
                self.build_command(environ={"SPIDER_NAME": "unknown"})

        self.assertEqual(captured.exception.code, 2)

    def test_main_executes_the_built_scrapy_command(self):
        main = getattr(self.start, "main", None)
        self.assertIsNotNone(main, "main must be defined")

        with patch.object(self.start.cmdline, "execute") as execute:
            main(["--spider", "myspider_redis"])

        execute.assert_called_once_with(
            ["scrapy", "crawl", "myspider_redis"]
        )

    def test_readme_documents_the_same_default_and_selectors(self):
        readme = (
            Path(__file__).resolve().parents[1] / "README.md"
        ).read_text(encoding="utf-8")

        self.assertIn("默认启动 `dmoz`", readme)
        self.assertIn("--spider", readme)
        self.assertIn("SPIDER_NAME", readme)


if __name__ == "__main__":
    unittest.main()
