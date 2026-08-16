"""两种浏览器采集器共用的输入策略与输出契约。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from taobao_collector.models import (
    CommentRecord,
    LiveRoomRecord,
    ProductRecord,
)


def _validate_pause_range(
    minimum: float,
    maximum: float,
    *,
    label: str,
) -> None:
    if not math.isfinite(minimum) or minimum < 0:
        raise ValueError(f"{label}最小停顿不能小于 0")
    if not math.isfinite(maximum) or maximum < minimum:
        raise ValueError(f"{label}最大停顿不能小于最小停顿")


class CollectionBlockedError(RuntimeError):
    """登录、验证码或风控页面阻止继续采集。"""

    def __init__(
        self,
        *,
        step: str,
        reason: str,
        screenshot_path: Optional[Path],
    ) -> None:
        super().__init__(f"{step}：{reason}")
        self.step = step
        self.reason = reason
        self.screenshot_path = screenshot_path


@dataclass(frozen=True)
class ProductFilterPolicy:
    """商品有效性筛选策略；关闭后忽略两个最低阈值。"""

    enabled: bool = True
    min_sales_count: int = 30
    min_comment_count: int = 20

    def __post_init__(self) -> None:
        if self.min_sales_count < 0:
            raise ValueError("最低销量不能小于 0")
        if self.min_comment_count < 0:
            raise ValueError("最低评论数不能小于 0")

    def accepts_sales(self, sales_count: Optional[int]) -> bool:
        return (
            not self.enabled
            or (
                sales_count is not None
                and sales_count >= self.min_sales_count
            )
        )

    def accepts_comments(self, comment_count: Optional[int]) -> bool:
        return (
            not self.enabled
            or (
                comment_count is not None
                and comment_count >= self.min_comment_count
            )
        )


@dataclass(frozen=True)
class ReadOnlyBehaviorPolicy:
    """商品详情页只读交互及辅助停顿的边界。"""

    enabled: bool = False
    min_pause: float = 3.0
    max_pause: float = 6.0
    comment_min_pause: float = 3.0
    comment_max_pause: float = 5.0
    product_min_pause: float = 5.0
    product_max_pause: float = 8.0
    page_min_pause: float = 30.0
    page_max_pause: float = 60.0
    max_scrolls: int = 2
    max_tab_views: int = 2

    def __post_init__(self) -> None:
        _validate_pause_range(
            self.min_pause,
            self.max_pause,
            label="详情页",
        )
        _validate_pause_range(
            self.comment_min_pause,
            self.comment_max_pause,
            label="评论加载",
        )
        _validate_pause_range(
            self.product_min_pause,
            self.product_max_pause,
            label="商品切换",
        )
        _validate_pause_range(
            self.page_min_pause,
            self.page_max_pause,
            label="商品翻页",
        )
        if self.max_scrolls < 0:
            raise ValueError("最大滚动次数不能小于 0")
        if self.max_tab_views < 0:
            raise ValueError("最大标签查看次数不能小于 0")


@dataclass(frozen=True)
class LiveBehaviorPolicy:
    """直播间只读停留和滚动策略。"""

    enabled: bool = False
    min_pause: float = 6.0
    max_pause: float = 10.0
    room_min_pause: float = 8.0
    room_max_pause: float = 12.0
    max_scrolls: int = 2

    def __post_init__(self) -> None:
        _validate_pause_range(
            self.min_pause,
            self.max_pause,
            label="直播间停留",
        )
        _validate_pause_range(
            self.room_min_pause,
            self.room_max_pause,
            label="直播间切换",
        )
        if self.max_scrolls < 0:
            raise ValueError("直播间最大滚动次数不能小于 0")


@dataclass(frozen=True)
class RetryPolicy:
    """首次尝试失败后的有限线性退避策略。"""

    max_retries: int = 3
    base_delay: float = 10.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("最大重试次数不能小于 0")
        if not math.isfinite(self.base_delay) or self.base_delay <= 0:
            raise ValueError("重试基础等待必须大于 0")

    @property
    def delays(self) -> Tuple[float, ...]:
        return tuple(
            self.base_delay * retry_number
            for retry_number in range(1, self.max_retries + 1)
        )

    @property
    def max_attempts(self) -> int:
        return self.max_retries + 1


@dataclass(frozen=True)
class WaitPolicy:
    """元素状态和异步数据变化的有界等待策略。"""

    timeout: float = 20.0
    poll_interval: float = 0.05

    def __post_init__(self) -> None:
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("等待超时必须大于 0")
        if (
            not math.isfinite(self.poll_interval)
            or self.poll_interval <= 0
        ):
            raise ValueError("轮询间隔必须大于 0")


@dataclass(frozen=True)
class CollectionResult:
    """一次采集调用产生的标准记录集合。"""

    products: Tuple[ProductRecord, ...] = ()
    comments: Tuple[CommentRecord, ...] = ()
    live_rooms: Tuple[LiveRoomRecord, ...] = ()


__all__ = [
    "CollectionBlockedError",
    "CollectionResult",
    "LiveBehaviorPolicy",
    "ProductFilterPolicy",
    "ReadOnlyBehaviorPolicy",
    "RetryPolicy",
    "WaitPolicy",
]
