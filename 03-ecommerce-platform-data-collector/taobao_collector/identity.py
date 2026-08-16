"""业务记录稳定唯一键与来源 URL 规范化。"""

from __future__ import annotations

import hashlib
import posixpath
import unicodedata
from typing import Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from taobao_collector.models import (
    CommentRecord,
    LiveRoomRecord,
    ProductRecord,
)


Record = Union[ProductRecord, CommentRecord, LiveRoomRecord]
_TRACKING_QUERY_NAMES = frozenset(
    {
        "ali_trackid",
        "pvid",
        "scm",
        "spm",
        "track_id",
        "wh_pid",
    }
)


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    return " ".join(normalized.split())


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _is_tracking_query(name: str) -> bool:
    lowered = name.casefold()
    return lowered in _TRACKING_QUERY_NAMES or lowered.startswith("utm_")


def normalize_url(value: str) -> str:
    """移除片段和常见跟踪参数，稳定主机、路径与查询顺序。"""

    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("来源 URL 格式无效") from exc
    scheme = parts.scheme.casefold()
    hostname = (parts.hostname or "").casefold()
    if not scheme or not hostname:
        return raw

    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("来源 URL 格式无效：端口非法") from exc
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = hostname
    if port is not None and not default_port:
        netloc = f"{netloc}:{port}"

    path = posixpath.normpath(parts.path or "/")
    if not path.startswith("/"):
        path = f"/{path}"
    if path != "/":
        path = path.rstrip("/")

    query_items = sorted(
        (name, item)
        for name, item in parse_qsl(
            parts.query,
            keep_blank_values=True,
        )
        if not _is_tracking_query(name)
    )
    return urlunsplit(
        (scheme, netloc, path, urlencode(query_items, doseq=True), "")
    )


def _product_key(product_id: str, source_url: str) -> str:
    normalized_id = _normalized_text(product_id)
    if normalized_id:
        return f"product:id:{normalized_id}"
    normalized_url = normalize_url(source_url)
    if not normalized_url:
        raise ValueError("商品记录缺少 product_id 和 source_url")
    return f"product:url:{normalized_url}"


def product_unique_key(record: ProductRecord) -> str:
    """优先按平台商品 ID，缺失时按规范化来源 URL 标识商品。"""

    return _product_key(record.product_id, record.source_url)


def comment_unique_key(record: CommentRecord) -> str:
    """按商品、用户标识摘要和评论内容摘要标识评论。"""

    product_key = _product_key(record.product_id, record.source_url)
    user_digest = _digest(_normalized_text(record.user_name))
    content_digest = _digest(_normalized_text(record.content))
    return f"comment:{product_key}:{user_digest}:{content_digest}"


def live_room_unique_key(record: LiveRoomRecord) -> str:
    """优先按直播间 ID，缺失时按规范化账号名称标识直播间。"""

    room_id = _normalized_text(record.live_room_id)
    if room_id:
        return f"live:id:{room_id}"
    account_name = _normalized_text(record.account_name)
    if not account_name:
        raise ValueError("直播间记录缺少 live_room_id 和 account_name")
    account_digest = _digest(account_name)
    return f"live:account:{account_digest}"


def record_unique_key(record: Record) -> str:
    """返回支持记录类型对应的稳定唯一键。"""

    if isinstance(record, ProductRecord):
        return product_unique_key(record)
    if isinstance(record, CommentRecord):
        return comment_unique_key(record)
    if isinstance(record, LiveRoomRecord):
        return live_room_unique_key(record)
    raise TypeError(f"不支持的记录类型：{type(record).__name__}")


__all__ = [
    "comment_unique_key",
    "live_room_unique_key",
    "normalize_url",
    "product_unique_key",
    "record_unique_key",
]
