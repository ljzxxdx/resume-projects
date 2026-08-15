"""商品、评论、直播间和运行摘要的统一数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class Engine(str, Enum):
    """支持的浏览器自动化引擎。"""

    DRISSION = "drission"
    SELENIUM = "selenium"


class RunMode(str, Enum):
    """统一入口支持的采集模式。"""

    PRODUCTS = "products"
    LIVE = "live"


class RecordStatus(str, Enum):
    """记录或运行的处理状态。"""

    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProductRecord:
    """规范化商品记录。"""

    run_id: str
    engine: Engine
    keyword: str
    product_id: str
    source_url: str
    name: str
    price: Optional[Decimal] = None
    sales_count: Optional[int] = None
    collected_at: datetime = field(default_factory=_utc_now)
    status: RecordStatus = RecordStatus.SUCCESS
    error_message: Optional[str] = None


@dataclass(frozen=True)
class CommentRecord:
    """与商品关联的规范化评论记录。"""

    run_id: str
    engine: Engine
    keyword: str
    product_id: str
    source_url: str
    user_name: str
    sku_info: str
    content: str
    collected_at: datetime = field(default_factory=_utc_now)
    status: RecordStatus = RecordStatus.SUCCESS
    error_message: Optional[str] = None


@dataclass(frozen=True)
class LiveRoomRecord:
    """规范化直播间记录。"""

    run_id: str
    engine: Engine
    keyword: str
    live_room_id: str
    source_url: str
    account_name: str
    introduction: str
    viewer_count: Optional[int] = None
    follower_count: Optional[int] = None
    product_count: Optional[int] = None
    collected_at: datetime = field(default_factory=_utc_now)
    status: RecordStatus = RecordStatus.SUCCESS
    error_message: Optional[str] = None


@dataclass(frozen=True)
class RunSummary:
    """一次采集任务的参数、时间和结果统计。"""

    run_id: str
    engine: Engine
    keyword: str
    mode: RunMode
    started_at: datetime = field(default_factory=_utc_now)
    ended_at: Optional[datetime] = None
    status: RecordStatus = RecordStatus.RUNNING
    parameters: Dict[str, Any] = field(default_factory=dict)
    product_count: int = 0
    comment_count: int = 0
    live_room_count: int = 0
    failed_count: int = 0
    new_count: int = 0
    success_count: int = 0
    duplicate_count: int = 0
    missing_count: int = 0
    screenshot_index: Tuple[str, ...] = ()
    error_message: Optional[str] = None
