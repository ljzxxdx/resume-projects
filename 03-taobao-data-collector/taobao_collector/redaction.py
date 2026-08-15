"""生成默认不公开评论正文的最小化脱敏样例。"""

from __future__ import annotations

import unicodedata
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Iterable, Mapping, Sequence, Set

from taobao_collector.identity import record_unique_key


def mask_user_name(value: str) -> str:
    """保留有限辨识度，同时遮盖名称主体。"""

    normalized = unicodedata.normalize("NFKC", value.strip())
    visible_characters = [
        character
        for character in normalized
        if (
            unicodedata.category(character) not in {"Cf", "Mn", "Me"}
            and not character.isspace()
        )
    ]
    if not visible_characters:
        return ""
    if len(visible_characters) == 1:
        return "*"
    return f"{visible_characters[0]}***"


def _public_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        return str(value)
    return value


def _approved_comments(
    comments: Sequence[Any],
    approved_keys: Set[str],
    limit: int,
) -> list:
    public_comments = []
    seen_keys = set()
    for comment in comments:
        try:
            key = record_unique_key(comment)
        except (TypeError, ValueError):
            continue
        if key not in approved_keys or key in seen_keys:
            continue
        seen_keys.add(key)
        public_comments.append(
            {
                "user_name": mask_user_name(comment.user_name),
                "sku_info": comment.sku_info,
                "content": comment.content,
                "status": _public_value(comment.status),
            }
        )
        if len(public_comments) >= limit:
            break
    return public_comments


def _unique_records(records: Sequence[Any], limit: int) -> list:
    unique = []
    seen_keys = set()
    for record in records:
        try:
            key = record_unique_key(record)
        except (TypeError, ValueError):
            continue
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique.append(record)
        if len(unique) >= limit:
            break
    return unique


def build_redacted_sample(
    records_by_table: Mapping[str, Sequence[Any]],
    *,
    approved_comment_keys: Iterable[str] = (),
    max_records_per_table: int = 3,
) -> Dict[str, Any]:
    """构造字段最少、条数受限且评论需显式批准的公开样例。"""

    if max_records_per_table <= 0:
        raise ValueError("脱敏样例条数上限必须大于 0")
    approved_keys = {
        key.strip()
        for key in approved_comment_keys
        if isinstance(key, str) and key.strip()
    }
    products = records_by_table.get("products", ())
    comments = records_by_table.get("comments", ())
    live_rooms = records_by_table.get("live_rooms", ())

    return {
        "publication_policy": {
            "comment_content": "explicit_unique_key_allowlist",
            "max_records_per_table": max_records_per_table,
        },
        "products": [
            {
                "name": product.name,
                "price": _public_value(product.price),
                "sales_count": product.sales_count,
                "status": _public_value(product.status),
            }
            for product in _unique_records(
                products,
                max_records_per_table,
            )
        ],
        "comments": _approved_comments(
            comments,
            approved_keys,
            max_records_per_table,
        ),
        "live_rooms": [
            {
                "account_name": mask_user_name(live_room.account_name),
                "viewer_count": live_room.viewer_count,
                "follower_count": live_room.follower_count,
                "product_count": live_room.product_count,
                "status": _public_value(live_room.status),
            }
            for live_room in _unique_records(
                live_rooms,
                max_records_per_table,
            )
        ],
    }


__all__ = ["build_redacted_sample", "mask_user_name"]
