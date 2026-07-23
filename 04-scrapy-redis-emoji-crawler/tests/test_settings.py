import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from scrapy.settings import Settings

from scrapyWithRedis.pipelines import JsonLinesPipeline, MetadataPipeline


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ENV_NAMES = (
    "REDIS_URL",
    "LOG_LEVEL",
    "CONCURRENT_REQUESTS",
    "DOWNLOAD_DELAY",
    "OUTPUT_DIR",
    "IMAGES_STORE",
    "WORKER_ID",
    "DMOZ_REDIS_BATCH_SIZE",
    "DMOZ_REDIS_MAX_IDLE_TIME",
)
DOTENV_PATH_ENV_NAME = "SCRAPY_DOTENV_PATH"


class RuntimeSettingsTests(unittest.TestCase):
    def load_settings_values(self, overrides=None):
        environment = os.environ.copy()
        for name in RUNTIME_ENV_NAMES:
            environment.pop(name, None)
        environment[DOTENV_PATH_ENV_NAME] = str(
            PROJECT_ROOT / ".env.test-does-not-exist"
        )
        environment.update(overrides or {})

        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import json; "
                    "from scrapyWithRedis import settings; "
                    "print(json.dumps({"
                    "name: getattr(settings, name) "
                    f"for name in {RUNTIME_ENV_NAMES!r}"
                    "}))"
                ),
            ],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_runtime_settings_are_loaded_from_dotenv_file(self):
        with TemporaryDirectory() as temporary_directory:
            dotenv_path = Path(temporary_directory) / ".env"
            dotenv_path.write_text(
                "\n".join(
                    (
                        "REDIS_URL=redis://redis:6379/3",
                        "LOG_LEVEL=WARNING",
                        "CONCURRENT_REQUESTS=6",
                        "DOWNLOAD_DELAY=1.5",
                        "OUTPUT_DIR=dotenv-output",
                        "IMAGES_STORE=dotenv-images",
                        "WORKER_ID=worker-from-dotenv",
                        "DMOZ_REDIS_BATCH_SIZE=7",
                        "DMOZ_REDIS_MAX_IDLE_TIME=45",
                    )
                ),
                encoding="utf-8",
            )

            values = self.load_settings_values(
                {DOTENV_PATH_ENV_NAME: str(dotenv_path)}
            )

        self.assertEqual(
            values["REDIS_URL"],
            "redis://redis:6379/3",
        )
        self.assertEqual(values["LOG_LEVEL"], "WARNING")
        self.assertEqual(values["CONCURRENT_REQUESTS"], 6)
        self.assertEqual(values["DOWNLOAD_DELAY"], 1.5)
        self.assertEqual(values["OUTPUT_DIR"], "dotenv-output")
        self.assertEqual(values["IMAGES_STORE"], "dotenv-images")
        self.assertEqual(values["WORKER_ID"], "worker-from-dotenv")
        self.assertEqual(values["DMOZ_REDIS_BATCH_SIZE"], 7)
        self.assertEqual(values["DMOZ_REDIS_MAX_IDLE_TIME"], 45)

    def test_process_environment_overrides_dotenv_file(self):
        with TemporaryDirectory() as temporary_directory:
            dotenv_path = Path(temporary_directory) / ".env"
            dotenv_path.write_text(
                (
                    "WORKER_ID=worker-from-dotenv\n"
                    "LOG_LEVEL=ERROR\n"
                ),
                encoding="utf-8",
            )

            values = self.load_settings_values(
                {
                    DOTENV_PATH_ENV_NAME: str(dotenv_path),
                    "WORKER_ID": "worker-from-process",
                }
            )

        self.assertEqual(values["WORKER_ID"], "worker-from-process")
        self.assertEqual(values["LOG_LEVEL"], "ERROR")

    def test_runtime_settings_can_be_overridden_by_environment(self):
        values = self.load_settings_values(
            {
                "REDIS_URL": "redis://redis.example:6380/2",
                "LOG_LEVEL": "DEBUG",
                "CONCURRENT_REQUESTS": "8",
                "DOWNLOAD_DELAY": "2.5",
                "OUTPUT_DIR": "worker-output",
                "IMAGES_STORE": "worker-images",
                "WORKER_ID": "worker-a",
                "DMOZ_REDIS_BATCH_SIZE": "9",
                "DMOZ_REDIS_MAX_IDLE_TIME": "60",
            }
        )

        self.assertEqual(values["REDIS_URL"], "redis://redis.example:6380/2")
        self.assertEqual(values["LOG_LEVEL"], "DEBUG")
        self.assertEqual(values["CONCURRENT_REQUESTS"], 8)
        self.assertEqual(values["DOWNLOAD_DELAY"], 2.5)
        self.assertEqual(values["OUTPUT_DIR"], "worker-output")
        self.assertEqual(values["IMAGES_STORE"], "worker-images")
        self.assertEqual(values["WORKER_ID"], "worker-a")
        self.assertEqual(values["DMOZ_REDIS_BATCH_SIZE"], 9)
        self.assertEqual(values["DMOZ_REDIS_MAX_IDLE_TIME"], 60)

    def test_default_redis_url_contains_no_credentials(self):
        values = self.load_settings_values()

        self.assertEqual(values["REDIS_URL"], "redis://127.0.0.1:6379/0")
        self.assertNotIn("@", values["REDIS_URL"])

    def test_json_lines_pipeline_reads_output_dir_setting(self):
        crawler = SimpleNamespace(
            settings=Settings(
                {
                    "OUTPUT_DIR": "custom-output",
                    "WORKER_ID": "worker-from-settings",
                }
            )
        )

        pipeline = JsonLinesPipeline.from_crawler(crawler)

        self.assertEqual(pipeline.output_dir, Path("custom-output"))
        self.assertTrue(
            pipeline.output_path.name.startswith(
                "items-worker-from-settings-"
            )
        )
        self.assertEqual(pipeline.output_path.suffix, ".jl")

    def test_metadata_pipeline_reads_worker_id_setting(self):
        crawler = SimpleNamespace(
            settings=Settings({"WORKER_ID": "worker-from-settings"})
        )
        spider = SimpleNamespace(name="dmoz")
        item = {}

        pipeline = MetadataPipeline.from_crawler(crawler)
        pipeline.process_item(item, spider)

        self.assertEqual(item["worker_id"], "worker-from-settings")

    def test_env_example_documents_runtime_settings_without_credentials(self):
        env_example = PROJECT_ROOT / ".env.example"

        self.assertTrue(env_example.is_file())
        content = env_example.read_text(encoding="utf-8")
        for name in RUNTIME_ENV_NAMES:
            self.assertIn(f"{name}=", content)

        redis_line = next(
            line for line in content.splitlines() if line.startswith("REDIS_URL=")
        )
        self.assertNotIn("@", redis_line)

    def test_real_env_file_is_ignored_but_example_is_kept(self):
        gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn(".env", gitignore.splitlines())
        self.assertIn("!.env.example", gitignore.splitlines())

    def test_repository_source_contains_no_plaintext_redis_credentials(self):
        credential_uri = re.compile("redis://" + r":[^@\s<>]+@")
        source_files = (
            list((PROJECT_ROOT / "scrapyWithRedis").rglob("*.py"))
            + list((PROJECT_ROOT / "tests").rglob("*.py"))
            + [
                PROJECT_ROOT / "start.py",
                PROJECT_ROOT / "README.md",
                PROJECT_ROOT / ".env.example",
            ]
        )

        matches = [
            path.relative_to(PROJECT_ROOT)
            for path in source_files
            if credential_uri.search(path.read_text(encoding="utf-8"))
        ]

        self.assertEqual(matches, [])


if __name__ == "__main__":
    unittest.main()
