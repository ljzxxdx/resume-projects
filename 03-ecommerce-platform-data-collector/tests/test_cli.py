"""Tests for command-line parsing and the root entry point."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Sequence
from unittest.mock import patch

from taobao_collector import cli
from taobao_collector.collectors.base import (
    CollectionBlockedError,
    CollectionResult,
)
from taobao_collector.config import AppConfig
from taobao_collector.identity import record_unique_key
from taobao_collector.models import (
    CommentRecord,
    Engine,
    LiveRoomRecord,
    ProductRecord,
    RecordStatus,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakePage:
    def __init__(self) -> None:
        self.screenshot_paths = []

    def get_screenshot(
        self,
        *,
        path: str,
        full_page: bool,
    ) -> None:
        screenshot_path = Path(path)
        screenshot_path.write_bytes(b"fake-live-evidence")
        self.screenshot_paths.append(screenshot_path)


class FakeBrowser:
    def __init__(self) -> None:
        self.latest_tab = FakePage()


class FakeSession:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.exit_types = []

    def __enter__(self) -> FakeBrowser:
        return self.browser

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.exit_types.append(exc_type)
        return False


def make_config(screenshot_dir: Path) -> AppConfig:
    return AppConfig.from_env(
        {"TAOBAO_SCREENSHOT_DIR": str(screenshot_dir)}
    )


class CliDependencyIsolationTests(unittest.TestCase):
    def test_drission_defaults_load_without_selenium_session_dependency(
        self,
    ) -> None:
        script = """
import builtins

real_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "undetected_chromedriver" or name.startswith(
        "undetected_chromedriver."
    ):
        raise ModuleNotFoundError(name)
    return real_import(name, *args, **kwargs)

builtins.__import__ = guarded_import

from taobao_collector import cli
from taobao_collector.models import Engine

assert cli._default_session_factory(Engine.DRISSION).__name__ == (
    "DrissionBrowserSession"
)
assert cli._default_collector_factory(Engine.DRISSION).__name__ == (
    "DrissionCollector"
)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr or completed.stdout,
        )


class CliParserTests(unittest.TestCase):
    def assert_parse_error(self, argv: Sequence[str]) -> None:
        error_output = io.StringIO()
        with redirect_stderr(error_output):
            with self.assertRaises(SystemExit) as raised:
                cli.parse_args(argv)
        self.assertEqual(raised.exception.code, 2)
        self.assertTrue(error_output.getvalue())

    def test_both_commands_accept_both_engines(self) -> None:
        for command in ("products", "live"):
            for engine in ("drission", "selenium"):
                with self.subTest(command=command, engine=engine):
                    args = cli.parse_args(
                        [command, "--engine", engine, "--keyword", "德化瓷"]
                    )
                    self.assertEqual(args.command, command)
                    self.assertEqual(args.engine, engine)

    def test_products_defaults_are_low_load(self) -> None:
        args = cli.parse_args(
            ["products", "--engine", "drission", "--keyword", "德化瓷"]
        )

        self.assertEqual(args.keyword, "德化瓷")
        self.assertEqual(args.product_limit, 5)
        self.assertEqual(args.comment_limit, 10)
        self.assertIsNone(args.timeout)
        self.assertEqual(args.output_dir, Path("artifacts"))
        self.assertTrue(args.quality_filter)
        self.assertEqual(args.min_sales, 30)
        self.assertEqual(args.min_comments, 20)
        self.assertEqual(args.approved_comment_key, [])
        self.assertFalse(hasattr(args, "live_limit"))

    def test_live_defaults_are_low_load(self) -> None:
        args = cli.parse_args(
            ["live", "--engine", "selenium", "--keyword", "苗族银饰"]
        )

        self.assertEqual(args.keyword, "苗族银饰")
        self.assertEqual(args.live_limit, 3)
        self.assertIsNone(args.timeout)
        self.assertEqual(args.output_dir, Path("artifacts"))
        self.assertFalse(hasattr(args, "product_limit"))
        self.assertFalse(hasattr(args, "comment_limit"))
        self.assertFalse(hasattr(args, "quality_filter"))
        self.assertFalse(hasattr(args, "min_sales"))
        self.assertFalse(hasattr(args, "min_comments"))
        self.assertFalse(hasattr(args, "approved_comment_key"))

    def test_products_accept_repeated_approved_comment_keys(self) -> None:
        args = cli.parse_args(
            [
                "products",
                "--engine",
                "drission",
                "--keyword",
                "德化瓷",
                "--approved-comment-key",
                "comment-key-1",
                "--approved-comment-key",
                "comment-key-2",
            ]
        )

        self.assertEqual(
            args.approved_comment_key,
            ["comment-key-1", "comment-key-2"],
        )

    def test_custom_values_are_converted(self) -> None:
        products = cli.parse_args(
            [
                "products",
                "--engine",
                "selenium",
                "--keyword",
                "瓷器",
                "--product-limit",
                "7",
                "--comment-limit",
                "12",
                "--timeout",
                "15.5",
                "--output-dir",
                "custom-output",
                "--no-quality-filter",
                "--min-sales",
                "0",
                "--min-comments",
                "6",
            ]
        )
        live = cli.parse_args(
            [
                "live",
                "--engine",
                "drission",
                "--keyword",
                "银饰",
                "--live-limit",
                "4",
                "--timeout",
                "9",
                "--output-dir",
                "live-output",
            ]
        )

        self.assertEqual(products.product_limit, 7)
        self.assertEqual(products.comment_limit, 12)
        self.assertEqual(products.timeout, 15.5)
        self.assertEqual(products.output_dir, Path("custom-output"))
        self.assertFalse(products.quality_filter)
        self.assertEqual(products.min_sales, 0)
        self.assertEqual(products.min_comments, 6)
        self.assertEqual(live.live_limit, 4)
        self.assertEqual(live.timeout, 9.0)
        self.assertEqual(live.output_dir, Path("live-output"))

    def test_keyword_is_trimmed(self) -> None:
        args = cli.parse_args(
            ["products", "--engine", "drission", "--keyword", "  德化瓷  "]
        )

        self.assertEqual(args.keyword, "德化瓷")

    def test_invalid_inputs_exit_with_code_two(self) -> None:
        invalid_commands = (
            [],
            ["products", "--keyword", "瓷器"],
            ["products", "--engine", "drission"],
            ["products", "--engine", "unknown", "--keyword", "瓷器"],
            ["products", "--engine", "drission", "--keyword", "   "],
            [
                "products",
                "--engine",
                "drission",
                "--keyword",
                "瓷器",
                "--product-limit",
                "0",
            ],
            [
                "products",
                "--engine",
                "drission",
                "--keyword",
                "瓷器",
                "--comment-limit",
                "-1",
            ],
            [
                "live",
                "--engine",
                "selenium",
                "--keyword",
                "银饰",
                "--live-limit",
                "not-a-number",
            ],
            [
                "live",
                "--engine",
                "selenium",
                "--keyword",
                "银饰",
                "--timeout",
                "0",
            ],
            [
                "live",
                "--engine",
                "selenium",
                "--keyword",
                "银饰",
                "--timeout",
                "nan",
            ],
            [
                "live",
                "--engine",
                "selenium",
                "--keyword",
                "银饰",
                "--timeout",
                "inf",
            ],
            [
                "products",
                "--engine",
                "drission",
                "--keyword",
                "瓷器",
                "--min-sales",
                "-1",
            ],
            [
                "products",
                "--engine",
                "drission",
                "--keyword",
                "瓷器",
                "--min-comments",
                "1.5",
            ],
        )

        for argv in invalid_commands:
            with self.subTest(argv=argv):
                self.assert_parse_error(argv)

    def test_subcommands_reject_each_others_limit_arguments(self) -> None:
        invalid_commands = (
            [
                "products",
                "--engine",
                "drission",
                "--keyword",
                "瓷器",
                "--live-limit",
                "3",
            ],
            [
                "live",
                "--engine",
                "selenium",
                "--keyword",
                "银饰",
                "--product-limit",
                "5",
            ],
            [
                "live",
                "--engine",
                "selenium",
                "--keyword",
                "银饰",
                "--comment-limit",
                "10",
            ],
            [
                "live",
                "--engine",
                "selenium",
                "--keyword",
                "银饰",
                "--no-quality-filter",
            ],
            [
                "live",
                "--engine",
                "selenium",
                "--keyword",
                "银饰",
                "--min-sales",
                "30",
            ],
        )

        for argv in invalid_commands:
            with self.subTest(argv=argv):
                self.assert_parse_error(argv)

    def test_help_lists_both_commands(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                cli.parse_args(["--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("products", output.getvalue())
        self.assertIn("live", output.getvalue())

    def test_main_passes_parsed_arguments_and_config_to_runner(self) -> None:
        settings = make_config(Path("screenshots"))
        received = []

        def fake_runner(args, config) -> int:
            received.append((args, config))
            return 7

        exit_code = cli.main(
            ["live", "--engine", "drission", "--keyword", "苗族银饰"],
            config=settings,
            runner=fake_runner,
        )

        self.assertEqual(exit_code, 7)
        self.assertEqual(received[0][0].command, "live")
        self.assertIs(received[0][1], settings)

    def test_root_entrypoint_help_does_not_start_browser(self) -> None:
        child_environment = os.environ.copy()
        child_environment["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "main.py"),
                "--help",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=child_environment,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("products", completed.stdout)
        self.assertIn("live", completed.stdout)
        self.assertEqual(completed.stderr, "")


class RunCommandTests(unittest.TestCase):
    def test_products_dispatches_policies_and_writes_success_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = FakeBrowser()
            session = FakeSession(browser)
            captured = {}

            class Collector:
                def __init__(self, active_browser, **kwargs) -> None:
                    captured["browser"] = active_browser
                    captured["policies"] = kwargs

                def collect_products(self, **kwargs) -> CollectionResult:
                    captured["call"] = kwargs
                    return CollectionResult(
                        products=(
                            ProductRecord(
                                run_id="run-products",
                                engine=Engine.DRISSION,
                                keyword="德化瓷",
                                product_id="1001",
                                source_url="https://item.taobao.com/1001",
                                name="茶杯",
                                price=Decimal("39.90"),
                            ),
                        ),
                        comments=(
                            CommentRecord(
                                run_id="run-products",
                                engine=Engine.DRISSION,
                                keyword="德化瓷",
                                product_id="1001",
                                source_url="https://item.taobao.com/1001",
                                user_name="用户甲",
                                sku_info="白色",
                                content="做工细致",
                            ),
                            CommentRecord(
                                run_id="run-products",
                                engine=Engine.DRISSION,
                                keyword="德化瓷",
                                product_id="1001",
                                source_url="https://item.taobao.com/1001",
                                user_name="用户乙",
                                sku_info="蓝色",
                                content="包装完整",
                            ),
                        ),
                    )

            approved_key = record_unique_key(
                CommentRecord(
                    run_id="different-run-does-not-affect-key",
                    engine=Engine.DRISSION,
                    keyword="德化瓷",
                    product_id="1001",
                    source_url="https://item.taobao.com/1001",
                    user_name="用户甲",
                    sku_info="白色",
                    content="做工细致",
                )
            )
            args = cli.parse_args(
                [
                    "products",
                    "--engine",
                    "drission",
                    "--keyword",
                    "德化瓷",
                    "--product-limit",
                    "1",
                    "--comment-limit",
                    "2",
                    "--timeout",
                    "4",
                    "--output-dir",
                    str(root / "output"),
                    "--min-sales",
                    "40",
                    "--min-comments",
                    "25",
                    "--approved-comment-key",
                    approved_key,
                ]
            )
            exit_code = cli.run_command(
                args,
                make_config(root / "screenshots"),
                session_factory=lambda config: session,
                collector_factory=Collector,
                run_id_factory=lambda: "run-products",
            )

            self.assertEqual(exit_code, 0)
            self.assertIs(captured["browser"], browser)
            self.assertEqual(
                captured["call"],
                {
                    "keyword": "德化瓷",
                    "product_limit": 1,
                    "comment_limit": 2,
                },
            )
            policies = captured["policies"]
            self.assertTrue(policies["filter_policy"].enabled)
            self.assertEqual(
                policies["filter_policy"].min_sales_count,
                40,
            )
            self.assertEqual(
                policies["filter_policy"].min_comment_count,
                25,
            )
            self.assertTrue(policies["behavior_policy"].enabled)
            self.assertTrue(policies["live_behavior_policy"].enabled)
            self.assertIsNotNone(policies["login_waiter"])
            self.assertEqual(policies["wait_policy"].timeout, 4.0)
            self.assertEqual(policies["retry_policy"].max_retries, 3)
            summary = json.loads(
                (
                    root
                    / "output"
                    / "run-products"
                    / "run_summary.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "success")
            self.assertEqual(summary["product_count"], 1)
            self.assertEqual(summary["comment_count"], 2)
            self.assertEqual(summary["new_count"], 3)
            self.assertEqual(summary["success_count"], 3)
            self.assertEqual(summary["duplicate_count"], 0)
            self.assertEqual(summary["missing_count"], 0)
            self.assertEqual(summary["failed_count"], 0)
            self.assertEqual(
                summary["parameters"]["product_limit"],
                1,
            )
            self.assertNotIn("approved_comment_key", summary["parameters"])
            self.assertEqual(
                summary["parameters"]["approved_comment_count"],
                1,
            )
            self.assertEqual(summary["screenshot_index"], [])
            self.assertTrue(
                (
                    root
                    / "output"
                    / "run-products"
                    / "screenshot_index.json"
                ).exists()
            )
            log_text = (
                root / "output" / "logs" / "run-products.log"
            ).read_text(encoding="utf-8")
            self.assertIn("run-products", log_text)
            self.assertIn("采集完成", log_text)
            product_rows = (
                root
                / "output"
                / "run-products"
                / "products.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            comment_rows = (
                root
                / "output"
                / "run-products"
                / "comments.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(product_rows), 1)
            self.assertEqual(len(comment_rows), 2)
            redacted_sample = json.loads(
                (
                    root
                    / "output"
                    / "run-products"
                    / "redacted_sample.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(len(redacted_sample["comments"]), 1)
            self.assertEqual(
                redacted_sample["comments"][0]["content"],
                "做工细致",
            )
            self.assertEqual(session.exit_types, [None])

    def test_live_dispatches_and_writes_live_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = FakeSession(FakeBrowser())
            calls = []
            policies = []

            class Collector:
                def __init__(self, browser, **kwargs) -> None:
                    policies.append(kwargs)

                def collect_live(self, **kwargs) -> CollectionResult:
                    calls.append(kwargs)
                    return CollectionResult(
                        live_rooms=(
                            LiveRoomRecord(
                                run_id="run-live",
                                engine=Engine.DRISSION,
                                keyword="苗族银饰",
                                live_room_id="live-1",
                                source_url="https://live.taobao.com/live-1",
                                account_name="主播甲",
                                introduction="银饰专场",
                            ),
                            LiveRoomRecord(
                                run_id="run-live",
                                engine=Engine.DRISSION,
                                keyword="苗族银饰",
                                live_room_id="live-2",
                                source_url="https://live.taobao.com/live-2",
                                account_name="主播乙",
                                introduction="手工银饰",
                            ),
                        ),
                    )

            args = cli.parse_args(
                [
                    "live",
                    "--engine",
                    "drission",
                    "--keyword",
                    "苗族银饰",
                    "--live-limit",
                    "2",
                    "--output-dir",
                    str(root / "output"),
                ]
            )
            settings = AppConfig.from_env(
                {
                    "TAOBAO_SCREENSHOT_DIR": str(
                        root / "screenshots"
                    ),
                    "TAOBAO_DEFAULT_WAIT": "7.5",
                }
            )
            exit_code = cli.run_command(
                args,
                settings,
                session_factory=lambda config: session,
                collector_factory=Collector,
                run_id_factory=lambda: "run-live",
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                calls,
                [{"keyword": "苗族银饰", "live_limit": 2}],
            )
            self.assertEqual(
                policies[0]["wait_policy"].timeout,
                7.5,
            )
            self.assertTrue(
                policies[0]["live_behavior_policy"].enabled
            )
            self.assertIsNotNone(policies[0]["login_waiter"])
            summary = json.loads(
                (
                    root / "output" / "run-live" / "run_summary.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["mode"], "live")
            self.assertEqual(summary["live_room_count"], 2)
            live_rows = (
                root
                / "output"
                / "run-live"
                / "live_rooms.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(live_rows), 2)

    def test_selenium_selects_selenium_runtime_and_writes_summary(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            driver = object()
            session = FakeSession(driver)
            received = {}

            class Collector:
                def __init__(self, *, driver, **kwargs) -> None:
                    received["driver"] = driver
                    received["kwargs"] = kwargs

                def collect_live(self, **kwargs) -> CollectionResult:
                    received["call"] = kwargs
                    return CollectionResult()

            args = cli.parse_args(
                [
                    "live",
                    "--engine",
                    "selenium",
                    "--keyword",
                    "苗族银饰",
                    "--output-dir",
                    str(root / "output"),
                ]
            )
            with patch.object(
                cli,
                "SeleniumBrowserSession",
                return_value=session,
            ) as session_class, patch.object(
                cli,
                "SeleniumCollector",
                Collector,
            ):
                exit_code = cli.run_command(
                    args,
                    make_config(root / "screenshots"),
                    run_id_factory=lambda: "run-selenium",
                )

            summary = json.loads(
                (
                    root
                    / "output"
                    / "run-selenium"
                    / "run_summary.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(exit_code, 0)
        session_class.assert_called_once()
        self.assertIs(received["driver"], driver)
        self.assertEqual(received["call"]["live_limit"], 3)
        self.assertEqual(summary["engine"], "selenium")
        self.assertEqual(summary["status"], "success")

    def test_blocked_collection_writes_blocked_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class Collector:
                def __init__(self, browser, **kwargs) -> None:
                    pass

                def collect_products(self, **kwargs) -> CollectionResult:
                    raise CollectionBlockedError(
                        step="product_detail",
                        reason="检测到安全验证",
                        screenshot_path=Path("blocked.png"),
                    )

            args = cli.parse_args(
                [
                    "products",
                    "--engine",
                    "drission",
                    "--keyword",
                    "德化瓷",
                    "--output-dir",
                    str(root / "output"),
                ]
            )
            errors = io.StringIO()
            with redirect_stderr(errors):
                exit_code = cli.run_command(
                    args,
                    make_config(root / "screenshots"),
                    session_factory=lambda config: FakeSession(
                        FakeBrowser()
                    ),
                    collector_factory=Collector,
                    run_id_factory=lambda: "run-blocked",
                )

            self.assertEqual(exit_code, 3)
            summary = json.loads(
                (
                    root
                    / "output"
                    / "run-blocked"
                    / "run_summary.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "blocked")
            self.assertIn("安全验证", summary["error_message"])
            self.assertIn("blocked.png", summary["error_message"])
            self.assertEqual(summary["failed_count"], 1)
            self.assertIn("blocked.png", errors.getvalue())

    def test_missing_or_failed_records_finish_as_partial(self) -> None:
        scenarios = (
            (
                "failed-record",
                ProductRecord(
                    run_id="run-partial",
                    engine=Engine.DRISSION,
                    keyword="陶瓷",
                    product_id="1001",
                    source_url="https://item.taobao.com/1001",
                    name="字段异常商品",
                    status=RecordStatus.FAILED,
                ),
                {"new_count": 1, "missing_count": 0, "failed_count": 1},
            ),
            (
                "missing-identity",
                ProductRecord(
                    run_id="run-partial",
                    engine=Engine.DRISSION,
                    keyword="陶瓷",
                    product_id="",
                    source_url="",
                    name="无身份商品",
                ),
                {"new_count": 0, "missing_count": 1, "failed_count": 0},
            ),
        )
        for name, record, expected in scenarios:
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)

                    class Collector:
                        def __init__(self, browser, **kwargs) -> None:
                            pass

                        def collect_products(self, **kwargs):
                            return CollectionResult(products=(record,))

                    args = cli.parse_args(
                        [
                            "products",
                            "--engine",
                            "drission",
                            "--keyword",
                            "陶瓷",
                            "--output-dir",
                            str(root / "output"),
                        ]
                    )
                    exit_code = cli.run_command(
                        args,
                        make_config(root / "screenshots"),
                        session_factory=lambda config: FakeSession(
                            FakeBrowser()
                        ),
                        collector_factory=Collector,
                        run_id_factory=lambda: "run-partial",
                    )
                    summary = json.loads(
                        (
                            root
                            / "output"
                            / "run-partial"
                            / "run_summary.json"
                        ).read_text(encoding="utf-8")
                    )

                self.assertEqual(exit_code, 4)
                self.assertEqual(summary["status"], "partial")
                for field, value in expected.items():
                    self.assertEqual(summary[field], value)
                self.assertEqual(summary["success_count"], 0)

    def test_keyboard_interrupt_writes_failure_and_exits_130(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = FakeSession(FakeBrowser())

            class Collector:
                def __init__(self, browser, **kwargs) -> None:
                    pass

                def collect_live(self, **kwargs) -> CollectionResult:
                    raise KeyboardInterrupt()

            args = cli.parse_args(
                [
                    "live",
                    "--engine",
                    "drission",
                    "--keyword",
                    "德化瓷",
                    "--output-dir",
                    str(root / "output"),
                ]
            )
            exit_code = cli.run_command(
                args,
                make_config(root / "screenshots"),
                session_factory=lambda config: session,
                collector_factory=Collector,
                run_id_factory=lambda: "run-interrupt",
            )

            self.assertEqual(exit_code, 130)
            self.assertEqual(session.exit_types, [KeyboardInterrupt])
            summary = json.loads(
                (
                    root
                    / "output"
                    / "run-interrupt"
                    / "run_summary.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["error_message"], "用户中断")
            self.assertEqual(summary["failed_count"], 1)

    def test_summary_write_error_does_not_mask_blocked_exit_code(
        self,
    ) -> None:
        class Collector:
            def __init__(self, browser, **kwargs) -> None:
                pass

            def collect_products(self, **kwargs) -> CollectionResult:
                raise CollectionBlockedError(
                    step="product_detail",
                    reason="检测到安全验证",
                    screenshot_path=None,
                )

        def fail_to_write(summary, output_dir):
            raise OSError("摘要目录不可写")

        args = cli.parse_args(
            [
                "products",
                "--engine",
                "drission",
                "--keyword",
                "德化瓷",
            ]
        )
        errors = io.StringIO()
        with redirect_stderr(errors):
            exit_code = cli.run_command(
                args,
                make_config(Path("screenshots")),
                session_factory=lambda config: FakeSession(
                    FakeBrowser()
                ),
                collector_factory=Collector,
                run_id_factory=lambda: "run-write-failure",
                summary_writer=fail_to_write,
            )

        self.assertEqual(exit_code, 3)
        self.assertIn("摘要目录不可写", errors.getvalue())

    def test_summary_keyboard_interrupt_still_closes_log_handlers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class Collector:
                def __init__(self, browser, **kwargs) -> None:
                    pass

                def collect_products(self, **kwargs) -> CollectionResult:
                    return CollectionResult()

            def interrupt_summary(summary, output_dir):
                raise KeyboardInterrupt()

            args = cli.parse_args(
                [
                    "products",
                    "--engine",
                    "drission",
                    "--keyword",
                    "陶瓷",
                    "--output-dir",
                    str(root / "output"),
                ]
            )
            with self.assertRaises(KeyboardInterrupt):
                cli.run_command(
                    args,
                    make_config(root / "screenshots"),
                    session_factory=lambda config: FakeSession(
                        FakeBrowser()
                    ),
                    collector_factory=Collector,
                    run_id_factory=lambda: "run-summary-interrupt",
                    summary_writer=interrupt_summary,
                )

            logger = logging.getLogger(
                "taobao_collector.run.run-summary-interrupt"
            )
            log_path = (
                root
                / "output"
                / "logs"
                / "run-summary-interrupt.log"
            )
            self.assertEqual(logger.handlers, [])
            log_path.unlink()
            self.assertFalse(log_path.exists())

    def test_collected_data_is_saved_before_browser_quit_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class QuitFailSession(FakeSession):
                def __exit__(
                    self,
                    exc_type,
                    exc_value,
                    traceback,
                ) -> bool:
                    raise RuntimeError("浏览器退出失败")

            class Collector:
                def __init__(self, browser, **kwargs) -> None:
                    pass

                def collect_products(self, **kwargs) -> CollectionResult:
                    return CollectionResult(
                        products=(
                            ProductRecord(
                                run_id="run-quit-failure",
                                engine=Engine.DRISSION,
                                keyword="陶瓷",
                                product_id="1001",
                                source_url="https://item.taobao.com/1001",
                                name="茶杯",
                            ),
                        )
                    )

            args = cli.parse_args(
                [
                    "products",
                    "--engine",
                    "drission",
                    "--keyword",
                    "陶瓷",
                    "--output-dir",
                    str(root / "output"),
                ]
            )

            exit_code = cli.run_command(
                args,
                make_config(root / "screenshots"),
                session_factory=lambda config: QuitFailSession(
                    FakeBrowser()
                ),
                collector_factory=Collector,
                run_id_factory=lambda: "run-quit-failure",
            )

            data_path = (
                root
                / "output"
                / "run-quit-failure"
                / "products.jsonl"
            )
            summary_path = (
                root
                / "output"
                / "run-quit-failure"
                / "run_summary.json"
            )
            self.assertEqual(exit_code, 1)
            self.assertTrue(data_path.exists())
            summary = json.loads(
                summary_path.read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["product_count"], 1)
            self.assertEqual(summary["new_count"], 1)
            self.assertEqual(summary["success_count"], 1)
            self.assertEqual(summary["failed_count"], 1)
            self.assertIn("浏览器退出失败", summary["error_message"])

    def test_live_failure_saves_unavailable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = FakeBrowser()

            class Collector:
                def __init__(self, active_browser, **kwargs) -> None:
                    pass

                def collect_live(self, **kwargs) -> CollectionResult:
                    raise RuntimeError("直播入口元素超时")

            args = cli.parse_args(
                [
                    "live",
                    "--engine",
                    "drission",
                    "--keyword",
                    "德化瓷",
                    "--output-dir",
                    str(root / "output"),
                ]
            )
            exit_code = cli.run_command(
                args,
                make_config(root / "screenshots"),
                session_factory=lambda config: FakeSession(browser),
                collector_factory=Collector,
                run_id_factory=lambda: "run-unavailable",
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(len(browser.latest_tab.screenshot_paths), 1)
            evidence = browser.latest_tab.screenshot_paths[0]
            self.assertEqual(
                evidence.relative_to(root / "screenshots").parts[:4],
                (
                    "run-unavailable",
                    "drission",
                    "live_unavailable",
                    evidence.name,
                ),
            )
            summary = json.loads(
                (
                    root
                    / "output"
                    / "run-unavailable"
                    / "run_summary.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(summary["failed_count"], 1)
            self.assertIn(str(evidence), summary["error_message"])

    def test_live_prefers_collector_evidence_over_main_tab_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            browser = FakeBrowser()
            detail_evidence = root / "detail-evidence.png"

            class Collector:
                def __init__(self, active_browser, **kwargs) -> None:
                    self.last_failure_screenshot = detail_evidence

                def collect_live(self, **kwargs) -> CollectionResult:
                    raise RuntimeError("直播详情解析失败")

            args = cli.parse_args(
                [
                    "live",
                    "--engine",
                    "drission",
                    "--keyword",
                    "德化瓷",
                    "--output-dir",
                    str(root / "output"),
                ]
            )
            errors = io.StringIO()
            with redirect_stderr(errors):
                exit_code = cli.run_command(
                    args,
                    make_config(root / "screenshots"),
                    session_factory=lambda config: FakeSession(browser),
                    collector_factory=Collector,
                    run_id_factory=lambda: "run-detail-evidence",
                )

            self.assertEqual(exit_code, 1)
            self.assertEqual(browser.latest_tab.screenshot_paths, [])
            self.assertIn(str(detail_evidence), errors.getvalue())


if __name__ == "__main__":
    unittest.main()
