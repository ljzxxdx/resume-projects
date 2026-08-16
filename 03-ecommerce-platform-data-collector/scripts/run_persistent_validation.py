"""在单个浏览器会话内串行执行低负载真实环境验收。"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path
from typing import Iterable, Sequence, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from taobao_collector.cli import parse_args, run_command
from taobao_collector.collectors.drission_session import (
    DrissionBrowserSession,
)
from taobao_collector.collectors.selenium_session import (
    SeleniumBrowserSession,
)
from taobao_collector.config import load_config


Scenario = Tuple[str, Sequence[str]]


def _product_arguments(
    engine: str,
    keyword: str,
    output_dir: Path,
) -> Sequence[str]:
    return (
        "products",
        "--engine",
        engine,
        "--keyword",
        keyword,
        "--product-limit",
        "5",
        "--comment-limit",
        "10",
        "--output-dir",
        str(output_dir),
    )


def _live_arguments(
    engine: str,
    keyword: str,
    output_dir: Path,
) -> Sequence[str]:
    return (
        "live",
        "--engine",
        engine,
        "--keyword",
        keyword,
        "--live-limit",
        "3",
        "--output-dir",
        str(output_dir),
    )


def build_scenarios(engine: str, root: Path) -> Tuple[Scenario, ...]:
    """返回按安全顺序执行的验收场景。"""

    engine_root = root / engine
    desktop_storage = _product_arguments(
        engine,
        "桌面收纳",
        engine_root / "desktop_storage",
    )
    return (
        ("products_desktop_storage", desktop_storage),
        (
            "products_canvas_bag",
            _product_arguments(
                engine,
                "帆布包",
                engine_root / "canvas_bag",
            ),
        ),
        ("products_desktop_storage_repeat", desktop_storage),
        (
            "live",
            _live_arguments(
                engine,
                "桌面收纳",
                engine_root / "live",
            ),
        ),
    )


def run_scenarios(engine: str, scenarios: Iterable[Scenario]) -> int:
    """复用一个浏览器进程运行场景，首次失败后立即停止。"""

    config = replace(
        load_config(PROJECT_ROOT / ".env"),
        verification_wait_timeout=900.0,
        max_scrolls=2,
    )
    if engine == "selenium":
        owner = SeleniumBrowserSession(config)
    else:
        owner = DrissionBrowserSession(config)

    with owner as browser:
        if engine == "selenium":
            session_factory = lambda current: SeleniumBrowserSession(
                current,
                driver=browser,
            )
        else:
            session_factory = lambda current: DrissionBrowserSession(
                current,
                browser=browser,
            )

        for name, argv in scenarios:
            print(f"[validation] 开始场景：{name}", flush=True)
            exit_code = run_command(
                parse_args(argv),
                config,
                session_factory=session_factory,
            )
            print(
                f"[validation] 场景结束：{name}，退出码={exit_code}",
                flush=True,
            )
            if exit_code != 0:
                return exit_code
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--engine",
        required=True,
        choices=("selenium", "drission"),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "validation-runs",
    )
    args = parser.parse_args(argv)
    return run_scenarios(
        args.engine,
        build_scenarios(args.engine, args.output_root),
    )


if __name__ == "__main__":
    raise SystemExit(main())
