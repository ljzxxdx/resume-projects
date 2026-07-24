import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LINUX_DIR = PROJECT_ROOT / "scripts" / "linux"


class LinuxScriptTests(unittest.TestCase):
    expected_scripts = (
        "init_env.sh",
        "enqueue_urls.sh",
        "start_workers.sh",
        "stop_workers.sh",
        "observe_redis.sh",
        "replay_page_failures.sh",
        "replay_image_failures.sh",
    )

    def test_all_shell_scripts_use_strict_bash_mode(self):
        for name in self.expected_scripts:
            with self.subTest(script=name):
                path = LINUX_DIR / name
                self.assertTrue(path.is_file(), f"missing {path}")
                content = path.read_text(encoding="utf-8")
                self.assertTrue(content.startswith("#!/usr/bin/env bash\n"))
                self.assertIn("set -euo pipefail", content)

    def test_requirements_pin_verified_direct_dependencies(self):
        content = (PROJECT_ROOT / "requirements.txt").read_text(
            encoding="utf-8"
        )
        for requirement in (
            "Scrapy==2.13.4",
            "scrapy-redis==0.9.1",
            "redis==7.0.1",
            "Pillow==11.3.0",
            "python-dotenv==1.2.1",
        ):
            self.assertIn(requirement, content)

    def test_worker_scripts_manage_two_pid_and_log_files_by_default(self):
        start = (LINUX_DIR / "start_workers.sh").read_text(encoding="utf-8")
        stop = (LINUX_DIR / "stop_workers.sh").read_text(encoding="utf-8")

        self.assertIn('WORKER_COUNT="${WORKER_COUNT:-2}"', start)
        self.assertIn("flock", start)
        self.assertIn(".pid", start)
        self.assertIn(".log", start)
        self.assertIn("nohup", start)
        self.assertIn("kill -0", start)
        self.assertIn("kill -TERM", stop)
        self.assertIn("/proc/", stop)

    def test_dmoz_workers_are_started_in_redis_queue_mode(self):
        start = (LINUX_DIR / "start_workers.sh").read_text(encoding="utf-8")

        self.assertIn('[[ "${SPIDER_NAME}" == "dmoz" ]]', start)
        self.assertIn('redis_start=true', start)

    def test_worker_processes_do_not_inherit_the_flock_descriptor(self):
        start = (LINUX_DIR / "start_workers.sh").read_text(encoding="utf-8")

        self.assertIn("9>&-", start)

    def test_replay_scripts_use_flock_to_prevent_cron_overlap(self):
        for name in (
            "replay_page_failures.sh",
            "replay_image_failures.sh",
        ):
            with self.subTest(script=name):
                content = (LINUX_DIR / name).read_text(encoding="utf-8")
                self.assertIn("flock -n", content)
                self.assertIn("failure_tasks.py", content)
                self.assertIn("--all", content)

    def test_scripts_do_not_contain_plaintext_redis_credentials(self):
        credential_uri = re.compile("redis://" + r":[^@\s<>]+@")
        matches = []
        for path in LINUX_DIR.glob("*"):
            if path.is_file() and credential_uri.search(
                path.read_text(encoding="utf-8")
            ):
                matches.append(path.name)
        self.assertEqual(matches, [])

    def test_crontab_example_has_page_and_image_replay(self):
        content = (LINUX_DIR / "crontab.example").read_text(encoding="utf-8")
        self.assertIn("replay_page_failures.sh", content)
        self.assertIn("replay_image_failures.sh", content)


if __name__ == "__main__":
    unittest.main()
