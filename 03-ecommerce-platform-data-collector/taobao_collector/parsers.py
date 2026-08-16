"""淘宝页面数值文本的纯函数解析器。"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional, Pattern


class NumericParseError(ValueError):
    """页面文本无法可靠地转换为目标数值。"""


_EMPTY_VALUES = frozenset({"", "--", "暂无", "n/a"})
_NUMBER = r"(?P<number>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
_UNIT = r"(?P<unit>千|万)?"
_PLUS = r"\+?"


def _compile_pattern(prefix: str, suffix: str) -> Pattern[str]:
    return re.compile(
        rf"^(?:{prefix})*{_NUMBER}{_UNIT}{_PLUS}(?:{suffix})*$",
        re.IGNORECASE,
    )


_PRICE_PATTERN = _compile_pattern(
    prefix=r"[¥￥]|价格|售价|到手价",
    suffix=r"元|人民币",
)
_SALES_PATTERN = _compile_pattern(
    prefix=r"已售|销量|月销|月售",
    suffix=r"人付款|件已售|件|笔",
)
_COMMENT_PATTERN = _compile_pattern(
    prefix=r"用户评价[·:：]?|评论[·:：]?|评价[·:：]?",
    suffix=r"条评价|条评论|条",
)
_VIEWER_PATTERN = _compile_pattern(
    prefix=r"观看人数|观看|在线",
    suffix=r"人观看|人在线|观看|人",
)
_FOLLOWER_PATTERN = _compile_pattern(
    prefix=r"粉丝|关注",
    suffix=r"位粉丝|粉丝|人关注|人",
)
_LIVE_PRODUCT_PATTERN = re.compile(
    rf"^(?:全部商品|全部宝贝|商品)?[（(]?"
    rf"{_NUMBER}{_UNIT}{_PLUS}[）)]?(?:件)?$",
    re.IGNORECASE,
)
_MULTIPLIERS = {
    None: Decimal(1),
    "千": Decimal(1000),
    "万": Decimal(10000),
}


def _normalize_text(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    if not isinstance(text, str):
        raise NumericParseError(f"数值文本必须是字符串或 None：{text!r}")

    normalized = re.sub(r"\s+", "", text)
    if normalized.casefold() in _EMPTY_VALUES:
        return None
    return normalized


def _parse_decimal(
    text: Optional[str],
    pattern: Pattern[str],
) -> Optional[Decimal]:
    normalized = _normalize_text(text)
    if normalized is None:
        return None

    match = pattern.fullmatch(normalized)
    if match is None:
        raise NumericParseError(f"无法解析数值文本：{text!r}")

    try:
        number = Decimal(match.group("number").replace(",", ""))
    except InvalidOperation as exc:
        raise NumericParseError(f"无法解析数值文本：{text!r}") from exc
    return number * _MULTIPLIERS[match.group("unit")]


def _parse_count(
    text: Optional[str],
    pattern: Pattern[str],
) -> Optional[int]:
    value = _parse_decimal(text, pattern)
    if value is None:
        return None
    if value != value.to_integral_value():
        raise NumericParseError(f"数量解析结果不是整数：{text!r}")
    return int(value)


def parse_price(text: Optional[str]) -> Optional[Decimal]:
    """解析商品价格；空值返回 ``None``。"""

    return _parse_decimal(text, _PRICE_PATTERN)


def parse_sales_count(text: Optional[str]) -> Optional[int]:
    """解析商品销量；空值返回 ``None``。"""

    return _parse_count(text, _SALES_PATTERN)


def parse_comment_count(text: Optional[str]) -> Optional[int]:
    """解析评论数量；空值返回 ``None``。"""

    return _parse_count(text, _COMMENT_PATTERN)


def parse_viewer_count(text: Optional[str]) -> Optional[int]:
    """解析直播观看人数；空值返回 ``None``。"""

    return _parse_count(text, _VIEWER_PATTERN)


def parse_follower_count(text: Optional[str]) -> Optional[int]:
    """解析直播粉丝数量；空值返回 ``None``。"""

    return _parse_count(text, _FOLLOWER_PATTERN)


def parse_live_product_count(text: Optional[str]) -> Optional[int]:
    """解析直播间商品标题中的商品数量；空值返回 ``None``。"""

    return _parse_count(text, _LIVE_PRODUCT_PATTERN)


__all__ = [
    "NumericParseError",
    "parse_price",
    "parse_sales_count",
    "parse_comment_count",
    "parse_viewer_count",
    "parse_follower_count",
    "parse_live_product_count",
]
