"""Tests for environment-backed application configuration."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from taobao_collector import config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AppConfigTests(unittest.TestCase):
    def test_defaults_are_safe_and_portable(self) -> None:
        settings = config.AppConfig.from_env({})

        self.assertEqual(settings.browser_port, 9333)
        self.assertIsNone(settings.browser_path)
        self.assertIsNone(
            getattr(settings, "chrome_major_version", "missing")
        )
        self.assertEqual(
            settings.user_data_dir,
            Path("artifacts/chrome-profile"),
        )
        self.assertFalse(settings.headless)
        self.assertEqual(settings.default_wait, 20.0)
        self.assertEqual(settings.max_retries, 3)
        self.assertEqual(settings.log_level, "INFO")
        self.assertEqual(
            settings.screenshot_dir,
            Path("artifacts/screenshots"),
        )
        self.assertTrue(settings.read_only_behavior)
        self.assertEqual(settings.min_pause, 3.0)
        self.assertEqual(settings.max_pause, 6.0)
        self.assertEqual(settings.comment_min_pause, 3.0)
        self.assertEqual(settings.comment_max_pause, 5.0)
        self.assertEqual(settings.product_min_pause, 5.0)
        self.assertEqual(settings.product_max_pause, 8.0)
        self.assertEqual(settings.page_min_pause, 30.0)
        self.assertEqual(settings.page_max_pause, 60.0)
        self.assertEqual(settings.max_scrolls, 2)
        self.assertEqual(settings.max_tab_views, 2)
        self.assertEqual(settings.retry_base_delay, 10.0)
        self.assertEqual(settings.poll_interval, 0.05)
        self.assertEqual(settings.login_wait_timeout, 180.0)
        self.assertEqual(settings.verification_wait_timeout, 300.0)
        self.assertTrue(settings.live_behavior)
        self.assertEqual(settings.live_min_pause, 6.0)
        self.assertEqual(settings.live_max_pause, 10.0)
        self.assertEqual(settings.live_room_min_pause, 8.0)
        self.assertEqual(settings.live_room_max_pause, 12.0)
        self.assertEqual(settings.live_max_scrolls, 2)

    def test_environment_values_are_converted_to_typed_fields(self) -> None:
        settings = config.AppConfig.from_env(
            {
                "TAOBAO_BROWSER_PORT": "9444",
                "TAOBAO_BROWSER_PATH": "browser/msedge.exe",
                "TAOBAO_CHROME_MAJOR_VERSION": "151",
                "TAOBAO_USER_DATA_DIR": "browser-profile",
                "TAOBAO_HEADLESS": "yes",
                "TAOBAO_DEFAULT_WAIT": "12.5",
                "TAOBAO_MAX_RETRIES": "0",
                "TAOBAO_LOG_LEVEL": "debug",
                "TAOBAO_SCREENSHOT_DIR": "evidence/screenshots",
                "TAOBAO_READ_ONLY_BEHAVIOR": "no",
                "TAOBAO_MIN_PAUSE": "0",
                "TAOBAO_MAX_PAUSE": "1.25",
                "TAOBAO_COMMENT_MIN_PAUSE": "1",
                "TAOBAO_COMMENT_MAX_PAUSE": "2",
                "TAOBAO_PRODUCT_MIN_PAUSE": "3",
                "TAOBAO_PRODUCT_MAX_PAUSE": "4",
                "TAOBAO_PAGE_MIN_PAUSE": "5",
                "TAOBAO_PAGE_MAX_PAUSE": "6",
                "TAOBAO_MAX_SCROLLS": "0",
                "TAOBAO_MAX_TAB_VIEWS": "4",
                "TAOBAO_RETRY_BASE_DELAY": "0.5",
                "TAOBAO_POLL_INTERVAL": "0.1",
                "TAOBAO_LOGIN_WAIT_TIMEOUT": "240",
                "TAOBAO_VERIFICATION_WAIT_TIMEOUT": "360",
                "TAOBAO_LIVE_BEHAVIOR": "false",
                "TAOBAO_LIVE_MIN_PAUSE": "1.5",
                "TAOBAO_LIVE_MAX_PAUSE": "3.5",
                "TAOBAO_LIVE_ROOM_MIN_PAUSE": "4.5",
                "TAOBAO_LIVE_ROOM_MAX_PAUSE": "5.5",
                "TAOBAO_LIVE_MAX_SCROLLS": "1",
            }
        )

        self.assertEqual(settings.browser_port, 9444)
        self.assertEqual(
            settings.browser_path,
            Path("browser/msedge.exe"),
        )
        self.assertEqual(
            getattr(settings, "chrome_major_version", None),
            151,
        )
        self.assertEqual(settings.user_data_dir, Path("browser-profile"))
        self.assertTrue(settings.headless)
        self.assertEqual(settings.default_wait, 12.5)
        self.assertEqual(settings.max_retries, 0)
        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(
            settings.screenshot_dir,
            Path("evidence/screenshots"),
        )
        self.assertFalse(settings.read_only_behavior)
        self.assertEqual(settings.min_pause, 0.0)
        self.assertEqual(settings.max_pause, 1.25)
        self.assertEqual(settings.comment_min_pause, 1.0)
        self.assertEqual(settings.comment_max_pause, 2.0)
        self.assertEqual(settings.product_min_pause, 3.0)
        self.assertEqual(settings.product_max_pause, 4.0)
        self.assertEqual(settings.page_min_pause, 5.0)
        self.assertEqual(settings.page_max_pause, 6.0)
        self.assertEqual(settings.max_scrolls, 0)
        self.assertEqual(settings.max_tab_views, 4)
        self.assertEqual(settings.retry_base_delay, 0.5)
        self.assertEqual(settings.poll_interval, 0.1)
        self.assertEqual(settings.login_wait_timeout, 240.0)
        self.assertEqual(settings.verification_wait_timeout, 360.0)
        self.assertFalse(settings.live_behavior)
        self.assertEqual(settings.live_min_pause, 1.5)
        self.assertEqual(settings.live_max_pause, 3.5)
        self.assertEqual(settings.live_room_min_pause, 4.5)
        self.assertEqual(settings.live_room_max_pause, 5.5)
        self.assertEqual(settings.live_max_scrolls, 1)

    def test_invalid_environment_values_are_rejected(self) -> None:
        invalid_values = (
            ("TAOBAO_BROWSER_PORT", "0"),
            ("TAOBAO_BROWSER_PORT", "65536"),
            ("TAOBAO_BROWSER_PORT", "not-a-number"),
            ("TAOBAO_CHROME_MAJOR_VERSION", "0"),
            ("TAOBAO_CHROME_MAJOR_VERSION", "not-a-number"),
            ("TAOBAO_HEADLESS", "sometimes"),
            ("TAOBAO_DEFAULT_WAIT", "0"),
            ("TAOBAO_DEFAULT_WAIT", "nan"),
            ("TAOBAO_DEFAULT_WAIT", "not-a-number"),
            ("TAOBAO_MAX_RETRIES", "-1"),
            ("TAOBAO_MAX_RETRIES", "1.5"),
            ("TAOBAO_LOG_LEVEL", "TRACE"),
            ("TAOBAO_SCREENSHOT_DIR", "   "),
            ("TAOBAO_READ_ONLY_BEHAVIOR", "sometimes"),
            ("TAOBAO_MIN_PAUSE", "-0.1"),
            ("TAOBAO_MIN_PAUSE", "nan"),
            ("TAOBAO_MAX_PAUSE", "-0.1"),
            ("TAOBAO_COMMENT_MIN_PAUSE", "-0.1"),
            ("TAOBAO_PRODUCT_MAX_PAUSE", "nan"),
            ("TAOBAO_PAGE_MIN_PAUSE", "-1"),
            ("TAOBAO_MAX_SCROLLS", "-1"),
            ("TAOBAO_MAX_TAB_VIEWS", "1.5"),
            ("TAOBAO_RETRY_BASE_DELAY", "0"),
            ("TAOBAO_POLL_INTERVAL", "inf"),
            ("TAOBAO_LOGIN_WAIT_TIMEOUT", "0"),
            ("TAOBAO_VERIFICATION_WAIT_TIMEOUT", "0"),
            ("TAOBAO_LIVE_BEHAVIOR", "sometimes"),
            ("TAOBAO_LIVE_MIN_PAUSE", "-1"),
            ("TAOBAO_LIVE_ROOM_MAX_PAUSE", "inf"),
            ("TAOBAO_LIVE_MAX_SCROLLS", "-1"),
        )

        for name, value in invalid_values:
            with self.subTest(name=name, value=value):
                with self.assertRaises(config.ConfigError):
                    config.AppConfig.from_env({name: value})

    def test_max_pause_cannot_be_less_than_min_pause(self) -> None:
        with self.assertRaises(config.ConfigError):
            config.AppConfig.from_env(
                {
                    "TAOBAO_MIN_PAUSE": "2",
                    "TAOBAO_MAX_PAUSE": "1",
                }
            )

        invalid_pairs = (
            ("TAOBAO_COMMENT_MIN_PAUSE", "TAOBAO_COMMENT_MAX_PAUSE"),
            ("TAOBAO_PRODUCT_MIN_PAUSE", "TAOBAO_PRODUCT_MAX_PAUSE"),
            ("TAOBAO_PAGE_MIN_PAUSE", "TAOBAO_PAGE_MAX_PAUSE"),
            ("TAOBAO_LIVE_ROOM_MIN_PAUSE", "TAOBAO_LIVE_ROOM_MAX_PAUSE"),
        )
        for minimum_name, maximum_name in invalid_pairs:
            with self.subTest(minimum_name=minimum_name):
                with self.assertRaises(config.ConfigError):
                    config.AppConfig.from_env(
                        {minimum_name: "2", maximum_name: "1"}
                    )

        with self.assertRaises(config.ConfigError):
            config.AppConfig.from_env(
                {
                    "TAOBAO_LIVE_MIN_PAUSE": "4",
                    "TAOBAO_LIVE_MAX_PAUSE": "3",
                }
            )

    def test_config_is_immutable_after_creation(self) -> None:
        settings = config.AppConfig.from_env({})

        with self.assertRaises(FrozenInstanceError):
            settings.max_retries = 9

    def test_dotenv_file_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "TAOBAO_BROWSER_PORT=9555",
                        "TAOBAO_HEADLESS=true",
                        "TAOBAO_LOG_LEVEL=warning",
                    )
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                settings = config.load_config(env_file)

        self.assertEqual(settings.browser_port, 9555)
        self.assertTrue(settings.headless)
        self.assertEqual(settings.log_level, "WARNING")

    def test_process_environment_takes_precedence_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "TAOBAO_BROWSER_PORT=9444\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"TAOBAO_BROWSER_PORT": "9666"},
                clear=True,
            ):
                settings = config.load_config(env_file)

        self.assertEqual(settings.browser_port, 9666)

    def test_env_example_contains_only_safe_configuration(self) -> None:
        example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        upper_example = example.upper()

        for name in (
            "TAOBAO_BROWSER_PORT",
            "TAOBAO_BROWSER_PATH",
            "TAOBAO_CHROME_MAJOR_VERSION",
            "TAOBAO_USER_DATA_DIR",
            "TAOBAO_HEADLESS",
            "TAOBAO_DEFAULT_WAIT",
            "TAOBAO_MAX_RETRIES",
            "TAOBAO_LOG_LEVEL",
            "TAOBAO_SCREENSHOT_DIR",
            "TAOBAO_READ_ONLY_BEHAVIOR",
            "TAOBAO_MIN_PAUSE",
            "TAOBAO_MAX_PAUSE",
            "TAOBAO_COMMENT_MIN_PAUSE",
            "TAOBAO_COMMENT_MAX_PAUSE",
            "TAOBAO_PRODUCT_MIN_PAUSE",
            "TAOBAO_PRODUCT_MAX_PAUSE",
            "TAOBAO_PAGE_MIN_PAUSE",
            "TAOBAO_PAGE_MAX_PAUSE",
            "TAOBAO_MAX_SCROLLS",
            "TAOBAO_MAX_TAB_VIEWS",
            "TAOBAO_RETRY_BASE_DELAY",
            "TAOBAO_POLL_INTERVAL",
            "TAOBAO_LOGIN_WAIT_TIMEOUT",
            "TAOBAO_VERIFICATION_WAIT_TIMEOUT",
            "TAOBAO_LIVE_BEHAVIOR",
            "TAOBAO_LIVE_MIN_PAUSE",
            "TAOBAO_LIVE_MAX_PAUSE",
            "TAOBAO_LIVE_ROOM_MIN_PAUSE",
            "TAOBAO_LIVE_ROOM_MAX_PAUSE",
            "TAOBAO_LIVE_MAX_SCROLLS",
        ):
            self.assertIn(f"{name}=", example)

        self.assertNotIn("COOKIE", upper_example)
        self.assertNotIn("PASSWORD", upper_example)
        self.assertNotIn("TOKEN", upper_example)
        self.assertIn(
            "TAOBAO_USER_DATA_DIR=artifacts/chrome-profile\n",
            example,
        )
        self.assertIn("TAOBAO_BROWSER_PATH=\n", example)
        self.assertIn("TAOBAO_CHROME_MAJOR_VERSION=\n", example)


if __name__ == "__main__":
    unittest.main()
