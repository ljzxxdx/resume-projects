"""统一命令行入口、运行编排和退出状态。"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from taobao_collector.collectors.base import (
    CollectionBlockedError,
    CollectionResult,
    LiveBehaviorPolicy,
    ProductFilterPolicy,
    ReadOnlyBehaviorPolicy,
    RetryPolicy,
    WaitPolicy,
)
from taobao_collector.config import AppConfig, load_config
from taobao_collector.exporters import write_collection_result
from taobao_collector.models import (
    Engine,
    RecordStatus,
    RunMode,
    RunSummary,
)
from taobao_collector.observability import (
    BlockGuard,
    ManualLoginWaiter,
    ScreenshotStore,
    close_run_logging,
    configure_run_logging,
    write_run_summary,
)
from taobao_collector.verification import ManualVerificationHandler


ENGINE_CHOICES = ("drission", "selenium")

# 保留为模块属性，便于测试或调用方注入；默认实现按所选引擎延迟加载。
DrissionCollector: Optional[Callable[..., Any]] = None
DrissionBrowserSession: Optional[Callable[..., Any]] = None
SeleniumCollector: Optional[Callable[..., Any]] = None
SeleniumBrowserSession: Optional[Callable[..., Any]] = None


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是非负整数") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是大于 0 的有限数字") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的有限数字")
    return parsed


def _non_empty_keyword(value: str) -> str:
    keyword = value.strip()
    if not keyword:
        raise argparse.ArgumentTypeError("关键词不能为空")
    return keyword


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--engine",
        required=True,
        choices=ENGINE_CHOICES,
        help="浏览器引擎：drission 或 selenium",
    )
    parser.add_argument(
        "--keyword",
        required=True,
        type=_non_empty_keyword,
        help="淘宝搜索关键词",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=None,
        help="页面等待超时秒数；未指定时使用环境配置",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("artifacts"),
        help="输出目录，默认 artifacts",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="淘宝商品、评论与直播数据采集"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{products,live}",
    )

    products_parser = subparsers.add_parser(
        "products",
        help="采集商品和评论",
    )
    _add_common_arguments(products_parser)
    products_parser.add_argument(
        "--product-limit",
        type=_positive_int,
        default=5,
        help="合格商品数量上限，默认 5",
    )
    products_parser.add_argument(
        "--comment-limit",
        type=_positive_int,
        default=10,
        help="每个合格商品的评论数量上限，默认 10",
    )
    quality_group = products_parser.add_mutually_exclusive_group()
    quality_group.add_argument(
        "--quality-filter",
        dest="quality_filter",
        action="store_true",
        default=True,
        help="应用最低销量和评论数筛选（默认启用）",
    )
    quality_group.add_argument(
        "--no-quality-filter",
        dest="quality_filter",
        action="store_false",
        help="关闭最低销量和评论数筛选",
    )
    products_parser.add_argument(
        "--min-sales",
        type=_non_negative_int,
        default=30,
        help="最低销量，默认 30",
    )
    products_parser.add_argument(
        "--min-comments",
        type=_non_negative_int,
        default=20,
        help="最低评论数；不足时不保留商品，默认 20",
    )
    products_parser.add_argument(
        "--approved-comment-key",
        action="append",
        default=[],
        help=(
            "人工确认可公开的评论唯一键；可重复指定，"
            "默认不公开任何评论正文"
        ),
    )

    live_parser = subparsers.add_parser(
        "live",
        help="采集直播间",
    )
    _add_common_arguments(live_parser)
    live_parser.add_argument(
        "--live-limit",
        type=_positive_int,
        default=3,
        help="直播间数量，默认 3",
    )

    return parser


def parse_args(
    argv: Optional[Sequence[str]] = None,
) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_run_id() -> str:
    return uuid.uuid4().hex


def _build_filter_policy(args: argparse.Namespace) -> ProductFilterPolicy:
    if args.command != "products":
        return ProductFilterPolicy(enabled=False)
    return ProductFilterPolicy(
        enabled=args.quality_filter,
        min_sales_count=args.min_sales,
        min_comment_count=args.min_comments,
    )


def _capture_live_unavailable(
    browser: Any,
    *,
    store: ScreenshotStore,
    run_id: str,
    engine: Engine,
) -> Optional[Path]:
    try:
        page = (
            browser
            if engine is Engine.SELENIUM
            else browser.latest_tab
        )
        return store.capture(
            page,
            run_id=run_id,
            engine=engine,
            step="live_unavailable",
        )
    except Exception:
        return None


def _run_collector(
    args: argparse.Namespace,
    collector: Any,
) -> CollectionResult:
    if args.command == "products":
        return collector.collect_products(
            keyword=args.keyword,
            product_limit=args.product_limit,
            comment_limit=args.comment_limit,
        )
    return collector.collect_live(
        keyword=args.keyword,
        live_limit=args.live_limit,
    )


def _persist_summary(
    summary: RunSummary,
    output_dir: Path,
    writer: Callable[[RunSummary, Path], Path],
    logger: Optional[logging.Logger] = None,
) -> Optional[Path]:
    try:
        return writer(summary, output_dir)
    except Exception as exc:
        message = f"运行摘要写入失败：{exc}"
        if logger is None:
            print(message, file=sys.stderr)
        else:
            logger.error(message)
        return None


def _parameter_summary(
    args: argparse.Namespace,
    config: AppConfig,
) -> dict:
    """保留可复现实验的非敏感有效参数。"""

    parameters = {
        "command": args.command,
        "engine": args.engine,
        "keyword": args.keyword,
        "timeout": (
            args.timeout
            if args.timeout is not None
            else config.default_wait
        ),
        "headless": config.headless,
        "max_retries": config.max_retries,
        "log_level": config.log_level,
    }
    for name in (
        "product_limit",
        "comment_limit",
        "live_limit",
        "quality_filter",
        "min_sales",
        "min_comments",
    ):
        if hasattr(args, name):
            parameters[name] = getattr(args, name)
    parameters["approved_comment_count"] = len(
        getattr(args, "approved_comment_key", ())
    )
    return parameters


def _read_export_stats(output_dir: Path, run_id: str) -> dict:
    path = Path(output_dir) / run_id / "export_stats.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {
        name: int(payload[name])
        for name in (
            "new_count",
            "success_count",
            "duplicate_count",
            "missing_count",
            "failed_count",
        )
    }


def _screenshot_index(
    store: ScreenshotStore,
    run_id: str,
    *extra_paths: Optional[Path],
) -> tuple:
    paths = list(store.paths_for(run_id))
    paths.extend(path for path in extra_paths if path is not None)
    return tuple(dict.fromkeys(str(path) for path in paths))


def _default_session_factory(engine: Engine) -> Callable[[AppConfig], Any]:
    global DrissionBrowserSession, SeleniumBrowserSession

    if engine is Engine.SELENIUM:
        if SeleniumBrowserSession is None:
            from taobao_collector.collectors.selenium_session import (
                SeleniumBrowserSession as session_class,
            )

            SeleniumBrowserSession = session_class
        return SeleniumBrowserSession

    if DrissionBrowserSession is None:
        from taobao_collector.collectors.drission_session import (
            DrissionBrowserSession as session_class,
        )

        DrissionBrowserSession = session_class
    return DrissionBrowserSession


def _default_collector_factory(engine: Engine) -> Callable[..., Any]:
    global DrissionCollector, SeleniumCollector

    if engine is Engine.SELENIUM:
        if SeleniumCollector is None:
            from taobao_collector.collectors.selenium_collector import (
                SeleniumCollector as collector_class,
            )

            SeleniumCollector = collector_class
        return SeleniumCollector

    if DrissionCollector is None:
        from taobao_collector.collectors.drission_collector import (
            DrissionCollector as collector_class,
        )

        DrissionCollector = collector_class
    return DrissionCollector


def _run_command_impl(
    args: argparse.Namespace,
    config: AppConfig,
    *,
    run_id: str,
    logger: logging.Logger,
    session_factory: Optional[Callable[[AppConfig], Any]] = None,
    collector_factory: Optional[Callable[..., Any]] = None,
    clock: Callable[[], datetime] = _utc_now,
    summary_writer: Callable[[RunSummary, Path], Path] = (
        write_run_summary
    ),
    result_writer: Callable[..., Any] = write_collection_result,
) -> int:
    """执行一个 CLI 任务并在每个结束分支写入运行摘要。"""

    mode = RunMode(args.command)
    engine = Engine(args.engine)
    active_session_factory = session_factory or _default_session_factory(
        engine
    )
    active_collector_factory = collector_factory or _default_collector_factory(
        engine
    )
    started_at = clock()
    summary = RunSummary(
        run_id=run_id,
        engine=engine,
        keyword=args.keyword,
        mode=mode,
        started_at=started_at,
        parameters=_parameter_summary(args, config),
    )
    screenshot_store = ScreenshotStore(config.screenshot_dir)
    unavailable_evidence: Optional[Path] = None
    result = CollectionResult()
    export_stats = {
        "new_count": 0,
        "success_count": 0,
        "duplicate_count": 0,
        "missing_count": 0,
        "failed_count": 0,
    }
    logger.info(
        "采集开始 run_id=%s mode=%s engine=%s keyword=%s",
        run_id,
        mode.value,
        args.engine,
        args.keyword,
    )

    try:
        with active_session_factory(config) as browser:
            collector_arguments = dict(
                run_id=run_id,
                filter_policy=_build_filter_policy(args),
                behavior_policy=ReadOnlyBehaviorPolicy(
                    enabled=config.read_only_behavior,
                    min_pause=config.min_pause,
                    max_pause=config.max_pause,
                    comment_min_pause=config.comment_min_pause,
                    comment_max_pause=config.comment_max_pause,
                    product_min_pause=config.product_min_pause,
                    product_max_pause=config.product_max_pause,
                    page_min_pause=config.page_min_pause,
                    page_max_pause=config.page_max_pause,
                    max_scrolls=config.max_scrolls,
                    max_tab_views=config.max_tab_views,
                ),
                live_behavior_policy=LiveBehaviorPolicy(
                    enabled=config.live_behavior,
                    min_pause=config.live_min_pause,
                    max_pause=config.live_max_pause,
                    room_min_pause=config.live_room_min_pause,
                    room_max_pause=config.live_room_max_pause,
                    max_scrolls=config.live_max_scrolls,
                ),
                retry_policy=RetryPolicy(
                    max_retries=config.max_retries,
                    base_delay=config.retry_base_delay,
                ),
                wait_policy=WaitPolicy(
                    timeout=(
                        args.timeout
                        if args.timeout is not None
                        else config.default_wait
                    ),
                    poll_interval=config.poll_interval,
                ),
                block_guard=BlockGuard(
                    screenshot_store,
                    verification_handler=ManualVerificationHandler(
                        timeout=config.verification_wait_timeout,
                        poll_interval=config.poll_interval,
                        notifier=logger.warning,
                    ),
                ),
                login_waiter=ManualLoginWaiter(
                    timeout=config.login_wait_timeout,
                    poll_interval=config.poll_interval,
                    screenshot_store=screenshot_store,
                    notifier=logger.warning,
                ),
            )
            if engine is Engine.SELENIUM:
                collector = active_collector_factory(
                    driver=browser,
                    **collector_arguments,
                )
            else:
                collector = active_collector_factory(
                    browser,
                    **collector_arguments,
                )
            try:
                result = _run_collector(args, collector)
            except (CollectionBlockedError, KeyboardInterrupt):
                raise
            except Exception:
                if args.command == RunMode.LIVE.value:
                    unavailable_evidence = getattr(
                        collector,
                        "last_failure_screenshot",
                        None,
                    )
                    if unavailable_evidence is None:
                        unavailable_evidence = (
                            _capture_live_unavailable(
                                browser,
                                store=screenshot_store,
                                run_id=run_id,
                                engine=engine,
                            )
                        )
                raise
            result_writer(
                result,
                args.output_dir,
                run_id=run_id,
                mode=mode,
                approved_comment_keys=getattr(
                    args,
                    "approved_comment_key",
                    (),
                ),
            )
            export_stats = _read_export_stats(args.output_dir, run_id)
    except CollectionBlockedError as exc:
        evidence = (
            f"，截图：{exc.screenshot_path}"
            if exc.screenshot_path is not None
            else ""
        )
        summary = replace(
            summary,
            ended_at=clock(),
            status=RecordStatus.BLOCKED,
            failed_count=max(1, export_stats["failed_count"]),
            new_count=export_stats["new_count"],
            success_count=export_stats["success_count"],
            duplicate_count=export_stats["duplicate_count"],
            missing_count=export_stats["missing_count"],
            screenshot_index=_screenshot_index(
                screenshot_store,
                run_id,
                exc.screenshot_path,
            ),
            error_message=f"{exc}{evidence}",
        )
        _persist_summary(
            summary,
            args.output_dir,
            summary_writer,
            logger,
        )
        logger.error("采集被阻塞：%s%s", exc, evidence)
        return 3
    except KeyboardInterrupt:
        summary = replace(
            summary,
            ended_at=clock(),
            status=RecordStatus.FAILED,
            product_count=len(result.products),
            comment_count=len(result.comments),
            live_room_count=len(result.live_rooms),
            failed_count=max(1, export_stats["failed_count"]),
            new_count=export_stats["new_count"],
            success_count=export_stats["success_count"],
            duplicate_count=export_stats["duplicate_count"],
            missing_count=export_stats["missing_count"],
            screenshot_index=_screenshot_index(
                screenshot_store,
                run_id,
            ),
            error_message="用户中断",
        )
        _persist_summary(
            summary,
            args.output_dir,
            summary_writer,
            logger,
        )
        logger.error("采集已由用户中断，浏览器资源已清理。")
        return 130
    except Exception as exc:
        evidence = (
            f"，不可用证据：{unavailable_evidence}"
            if unavailable_evidence is not None
            else ""
        )
        summary = replace(
            summary,
            ended_at=clock(),
            status=RecordStatus.FAILED,
            product_count=len(result.products),
            comment_count=len(result.comments),
            live_room_count=len(result.live_rooms),
            failed_count=max(1, export_stats["failed_count"]),
            new_count=export_stats["new_count"],
            success_count=export_stats["success_count"],
            duplicate_count=export_stats["duplicate_count"],
            missing_count=export_stats["missing_count"],
            screenshot_index=_screenshot_index(
                screenshot_store,
                run_id,
                unavailable_evidence,
            ),
            error_message=f"{exc}{evidence}",
        )
        _persist_summary(
            summary,
            args.output_dir,
            summary_writer,
            logger,
        )
        logger.error("采集失败：%s%s", exc, evidence)
        return 1

    partial = bool(
        export_stats["failed_count"] or export_stats["missing_count"]
    )
    summary = replace(
        summary,
        ended_at=clock(),
        status=(
            RecordStatus.PARTIAL
            if partial
            else RecordStatus.SUCCESS
        ),
        product_count=len(result.products),
        comment_count=len(result.comments),
        live_room_count=len(result.live_rooms),
        failed_count=export_stats["failed_count"],
        new_count=export_stats["new_count"],
        success_count=export_stats["success_count"],
        duplicate_count=export_stats["duplicate_count"],
        missing_count=export_stats["missing_count"],
        screenshot_index=_screenshot_index(
            screenshot_store,
            run_id,
        ),
    )
    summary_path = _persist_summary(
        summary,
        args.output_dir,
        summary_writer,
        logger,
    )
    if summary_path is None:
        return 1
    log_method = logger.warning if partial else logger.info
    completion_label = "采集部分完成" if partial else "采集完成"
    log_method(
        "%s：run_id=%s，商品=%d，评论=%d，直播间=%d，"
        "新增=%d，成功=%d，重复=%d，缺失=%d，失败=%d，"
        "数据目录=%s，摘要=%s",
        completion_label,
        run_id,
        len(result.products),
        len(result.comments),
        len(result.live_rooms),
        export_stats["new_count"],
        export_stats["success_count"],
        export_stats["duplicate_count"],
        export_stats["missing_count"],
        export_stats["failed_count"],
        summary_path.parent,
        summary_path,
    )
    return 4 if partial else 0


def run_command(
    args: argparse.Namespace,
    config: AppConfig,
    *,
    session_factory: Optional[Callable[[AppConfig], Any]] = None,
    collector_factory: Optional[Callable[..., Any]] = None,
    run_id_factory: Callable[[], str] = _new_run_id,
    clock: Callable[[], datetime] = _utc_now,
    summary_writer: Callable[[RunSummary, Path], Path] = (
        write_run_summary
    ),
    result_writer: Callable[..., Any] = write_collection_result,
) -> int:
    """执行一次任务，并在任何退出路径关闭本次日志处理器。"""

    run_id = run_id_factory()
    logger = configure_run_logging(
        args.output_dir,
        run_id=run_id,
        level=config.log_level,
    )
    try:
        return _run_command_impl(
            args,
            config,
            run_id=run_id,
            logger=logger,
            session_factory=session_factory,
            collector_factory=collector_factory,
            clock=clock,
            summary_writer=summary_writer,
            result_writer=result_writer,
        )
    finally:
        close_run_logging(logger)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    config: Optional[AppConfig] = None,
    runner: Callable[[argparse.Namespace, AppConfig], int] = run_command,
) -> int:
    args = parse_args(argv)
    settings = config if config is not None else load_config()
    return runner(args, settings)


__all__ = [
    "build_parser",
    "main",
    "parse_args",
    "run_command",
]
