"""为规范化采集记录生成与网站无关的数据质量统计。"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Mapping, Sequence, Tuple

from taobao_collector.models import RecordStatus


_TABLES = ("products", "comments", "live_rooms")
_REQUIRED_RULES: Dict[str, Tuple[Tuple[str, Tuple[str, ...]], ...]] = {
    "products": (
        ("run_id", ("run_id",)),
        ("engine", ("engine",)),
        ("keyword", ("keyword",)),
        ("business_identity", ("product_id", "source_url")),
        ("name", ("name",)),
        ("collected_at", ("collected_at",)),
        ("status", ("status",)),
    ),
    "comments": (
        ("run_id", ("run_id",)),
        ("engine", ("engine",)),
        ("keyword", ("keyword",)),
        ("business_identity", ("product_id", "source_url")),
        ("user_name", ("user_name",)),
        ("content", ("content",)),
        ("collected_at", ("collected_at",)),
        ("status", ("status",)),
    ),
    "live_rooms": (
        ("run_id", ("run_id",)),
        ("engine", ("engine",)),
        ("keyword", ("keyword",)),
        (
            "business_identity",
            ("live_room_id", "account_name"),
        ),
        ("source_url", ("source_url",)),
        ("account_name", ("account_name",)),
        ("collected_at", ("collected_at",)),
        ("status", ("status",)),
    ),
}


def _is_missing(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and not value.strip()
    )


def _missing_required_fields(
    table: str,
    records: Sequence[Any],
) -> Counter:
    missing = Counter()
    for record in records:
        for label, alternatives in _REQUIRED_RULES[table]:
            if all(
                _is_missing(getattr(record, field_name, None))
                for field_name in alternatives
            ):
                missing[label] += 1
    return missing


def build_data_quality(
    records_by_table: Mapping[str, Sequence[Any]],
    export_stats_by_table: Mapping[str, Mapping[str, int]],
) -> Dict[str, Any]:
    """生成稳定三表结构的数据质量报告。"""

    report: Dict[str, Any] = {"tables": {}}
    for table in _TABLES:
        records = tuple(records_by_table.get(table, ()))
        export_stats = export_stats_by_table.get(table, {})
        missing = _missing_required_fields(table, records)
        report["tables"][table] = {
            "record_count": len(records),
            "unique_count": int(export_stats.get("new_count", 0)),
            "duplicate_count": int(
                export_stats.get("duplicate_count", 0)
            ),
            "required_field_missing_count": sum(missing.values()),
            "required_field_missing_by_field": dict(sorted(missing.items())),
            "abnormal_status_count": sum(
                getattr(record, "status", None)
                is not RecordStatus.SUCCESS
                for record in records
            ),
        }
    return report


__all__ = ["build_data_quality"]
