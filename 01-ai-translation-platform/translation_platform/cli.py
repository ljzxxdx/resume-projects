"""AI 翻译平台的命令行入口。"""

from __future__ import annotations

import argparse
import math
from typing import Optional, Sequence

from translation_platform.config import SUPPORTED_LANGUAGES
from translation_platform.validation import InputValidationError, validate_arguments


def _bounded_integer(option: str, minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{option} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{option} must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


def _bounded_number(option: str, minimum: float, maximum: float):
    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{option} must be a number") from exc
        if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{option} must be between {minimum:g} and {maximum:g}"
            )
        return parsed

    return parse


def build_parser() -> argparse.ArgumentParser:
    """构建单条翻译和批量翻译请求的命令解析器。"""

    parser = argparse.ArgumentParser(
        prog="ai-translation-platform",
        description="Low-load command-line client for an AI translation platform.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    translate_parser = subparsers.add_parser(
        "translate",
        help="validate one translation request",
    )
    translate_parser.add_argument("--text", required=True)
    translate_parser.add_argument(
        "--from-lang",
        choices=SUPPORTED_LANGUAGES,
    )
    translate_parser.add_argument(
        "--to-lang",
        choices=SUPPORTED_LANGUAGES,
    )

    batch_parser = subparsers.add_parser(
        "batch",
        help="validate one CSV or XLSX batch request",
    )
    batch_parser.add_argument("--input", dest="input_path", required=True)
    batch_parser.add_argument("--columns", required=True)
    batch_parser.add_argument("--output", dest="output_path", required=True)
    batch_parser.add_argument(
        "--from-lang",
        choices=SUPPORTED_LANGUAGES,
        default="zh-CHS",
    )
    batch_parser.add_argument(
        "--to-lang",
        choices=SUPPORTED_LANGUAGES,
        default="en",
    )
    batch_parser.add_argument(
        "--workers",
        type=_bounded_integer("--workers", 1, 2),
        default=1,
    )
    batch_parser.add_argument(
        "--min-interval",
        type=_bounded_number("--min-interval", 1, 60),
        default=1.0,
    )
    batch_parser.add_argument(
        "--max-retries",
        type=_bounded_integer("--max-retries", 0, 5),
        default=3,
    )
    batch_parser.add_argument("--checkpoint", dest="checkpoint_path")
    batch_parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="显式重试检查点中已有的失败记录",
    )
    batch_parser.add_argument("--use-proxy", action="store_true")

    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """解析、规范化并校验命令参数。"""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return validate_arguments(arguments)
    except InputValidationError as exc:
        parser.error(str(exc))
        raise AssertionError("argparse.error() did not exit")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """校验命令参数并返回进程退出码。"""

    parse_args(argv)
    return 0
