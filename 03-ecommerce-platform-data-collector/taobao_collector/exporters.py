"""将一次采集结果写入按 run_id 隔离的多格式数据表。"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Set, Tuple, Type

from openpyxl import Workbook

from taobao_collector.collectors.base import CollectionResult
from taobao_collector.identity import record_unique_key
from taobao_collector.models import (
    CommentRecord,
    LiveRoomRecord,
    ProductRecord,
    RecordStatus,
    RunMode,
)
from taobao_collector.quality import build_data_quality
from taobao_collector.redaction import build_redacted_sample


_INDEX_FILENAME = ".dedup_keys.json"
_LOCK_FILENAME = ".export.lock"
_RUN_KEYS_FILENAME = ".unique_keys.json"
_INDEX_TABLES = ("products", "comments", "live_rooms")


def _safe_segment(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "_", value.strip())
    return normalized.strip("_") or "unknown"


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _write_jsonl(path: Path, records: Iterable[Any]) -> Path:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            line = json.dumps(
                _record_payload(record),
                ensure_ascii=False,
            )
            stream.write(f"{line}\n")
    return path


def _record_payload(record: Any) -> Dict[str, Any]:
    payload = _json_value(asdict(record))
    payload["unique_key"] = record_unique_key(record)
    return payload


def _table_field_names(record_type: Type[Any]) -> List[str]:
    return [field.name for field in fields(record_type)] + ["unique_key"]


def _spreadsheet_value(value: Any) -> Any:
    if (
        isinstance(value, str)
        and value.startswith(("=", "+", "-", "@"))
    ):
        return f"'{value}"
    return value


def _spreadsheet_payload(record: Any) -> Dict[str, Any]:
    return {
        key: _spreadsheet_value(value)
        for key, value in _record_payload(record).items()
    }


def _write_csv(
    path: Path,
    records: Iterable[Any],
    record_type: Type[Any],
) -> Path:
    field_names = _table_field_names(record_type)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=field_names)
        writer.writeheader()
        for record in records:
            writer.writerow(_spreadsheet_payload(record))
    return path


def _write_excel(
    path: Path,
    records: Iterable[Any],
    record_type: Type[Any],
    *,
    sheet_name: str,
) -> Path:
    field_names = _table_field_names(record_type)
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title=sheet_name)
    sheet.append(field_names)
    for record in records:
        payload = _spreadsheet_payload(record)
        sheet.append([payload[name] for name in field_names])
    workbook.save(path)
    return path


def _validate_key_payload(payload: Any) -> Dict[str, Set[str]]:
    if (
        not isinstance(payload, dict)
        or set(payload) != set(_INDEX_TABLES)
    ):
        raise ValueError("去重索引格式无效")

    index: Dict[str, Set[str]] = {}
    for table in _INDEX_TABLES:
        values = payload[table]
        if (
            not isinstance(values, list)
            or any(
                not isinstance(value, str) or not value
                for value in values
            )
        ):
            raise ValueError("去重索引格式无效")
        index[table] = set(values)
    return index


def _read_key_file(path: Path) -> Dict[str, Set[str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("去重索引格式无效") from exc
    return _validate_key_payload(payload)


def _load_key_index(path: Path) -> Dict[str, Set[str]]:
    """读取缓存，并合并每个已发布运行目录中的权威键清单。"""

    if path.exists():
        index = _read_key_file(path)
    else:
        index = {table: set() for table in _INDEX_TABLES}

    for manifest_path in path.parent.glob(
        f"*/{_RUN_KEYS_FILENAME}"
    ):
        if manifest_path.parent.name.startswith("."):
            continue
        manifest = _read_key_file(manifest_path)
        for table in _INDEX_TABLES:
            index[table].update(manifest[table])
    return index


def _try_acquire_file_lock(stream: Any) -> bool:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _release_file_lock(stream: Any) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _exclusive_export_lock(
    output_root: Path,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.05,
):
    """序列化同一输出目录的索引读取与结果发布。"""

    lock_path = output_root / _LOCK_FILENAME
    deadline = time.monotonic() + timeout
    lock_stream = lock_path.open("a+b")
    lock_stream.seek(0, os.SEEK_END)
    if lock_stream.tell() == 0:
        lock_stream.write(b"\0")
        lock_stream.flush()

    acquired = False
    while not acquired:
        acquired = _try_acquire_file_lock(lock_stream)
        if not acquired:
            if time.monotonic() >= deadline:
                lock_stream.close()
                raise TimeoutError(
                    f"等待导出锁超时：{lock_path}"
                )
            time.sleep(poll_interval)

    try:
        yield
    finally:
        _release_file_lock(lock_stream)
        lock_stream.close()


def _deduplicate_records(
    records: Sequence[Any],
    existing_keys: Set[str],
) -> Tuple[Tuple[Any, ...], Dict[str, int], Set[str]]:
    accepted: List[Any] = []
    current_keys: Set[str] = set()
    new_keys: Set[str] = set()
    stats = {
        "new_count": 0,
        "success_count": 0,
        "duplicate_count": 0,
        "missing_count": 0,
        "failed_count": 0,
    }
    for record in records:
        try:
            key = record_unique_key(record)
        except ValueError:
            stats["missing_count"] += 1
            continue

        first_in_batch = key not in current_keys
        if first_in_batch and record.status is not RecordStatus.SUCCESS:
            stats["failed_count"] += 1
        if key in existing_keys or not first_in_batch:
            stats["duplicate_count"] += 1
            current_keys.add(key)
            continue

        current_keys.add(key)
        new_keys.add(key)
        accepted.append(record)
        stats["new_count"] += 1
        if record.status is RecordStatus.SUCCESS:
            stats["success_count"] += 1
    return tuple(accepted), stats, new_keys


def _stats_payload(table_stats: Dict[str, Dict[str, int]]) -> Dict[str, Any]:
    totals = {
        name: sum(stats[name] for stats in table_stats.values())
        for name in (
            "new_count",
            "success_count",
            "duplicate_count",
            "missing_count",
            "failed_count",
        )
    }
    return {**totals, "tables": table_stats}


def _write_key_index(path: Path, keys: Dict[str, Set[str]]) -> None:
    payload = {
        table: sorted(values)
        for table, values in keys.items()
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_collection_result(
    result: CollectionResult,
    output_dir: Path,
    *,
    run_id: str,
    mode: RunMode,
    approved_comment_keys: Iterable[str] = (),
) -> Tuple[Path, ...]:
    """按任务模式写入明细；即使无记录也创建对应空文件。"""

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    with _exclusive_export_lock(output_root):
        return _write_collection_result_locked(
            result,
            output_root,
            run_id=run_id,
            mode=mode,
            approved_comment_keys=approved_comment_keys,
        )


def _write_collection_result_locked(
    result: CollectionResult,
    output_root: Path,
    *,
    run_id: str,
    mode: RunMode,
    approved_comment_keys: Iterable[str] = (),
) -> Tuple[Path, ...]:
    """在持有输出目录互斥锁时完成一次原子导出。"""

    directory = output_root / _safe_segment(run_id)
    if directory.exists():
        raise FileExistsError(f"运行输出目录已存在：{directory}")

    index_path = output_root / _INDEX_FILENAME
    key_index = _load_key_index(index_path)
    if mode is RunMode.PRODUCTS:
        source_tables = (
            ("products", result.products, ProductRecord),
            ("comments", result.comments, CommentRecord),
        )
    else:
        source_tables = (
            ("live_rooms", result.live_rooms, LiveRoomRecord),
        )

    files = []
    table_stats: Dict[str, Dict[str, int]] = {}
    new_keys_by_table: Dict[str, Set[str]] = {}
    for table, records, record_type in source_tables:
        accepted, stats, new_keys = _deduplicate_records(
            records,
            key_index[table],
        )
        files.append((table, accepted, record_type))
        table_stats[table] = stats
        new_keys_by_table[table] = new_keys

    temporary_index = output_root / (
        f".{_INDEX_FILENAME}.{uuid.uuid4().hex}.tmp"
    )
    temporary_directory = Path(
        tempfile.mkdtemp(
            prefix=f".{_safe_segment(run_id)}-",
            dir=output_root,
        )
    )

    try:
        published_filenames = []
        for table, records, record_type in files:
            jsonl_name = f"{table}.jsonl"
            csv_name = f"{table}.csv"
            excel_name = f"{table}.xlsx"
            _write_jsonl(temporary_directory / jsonl_name, records)
            _write_csv(
                temporary_directory / csv_name,
                records,
                record_type,
            )
            _write_excel(
                temporary_directory / excel_name,
                records,
                record_type,
                sheet_name=table,
            )
            published_filenames.extend(
                (jsonl_name, csv_name, excel_name)
            )
        (temporary_directory / "export_stats.json").write_text(
            json.dumps(
                _stats_payload(table_stats),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        quality = build_data_quality(
            {
                table: records
                for table, records, _ in source_tables
            },
            table_stats,
        )
        (temporary_directory / "data_quality.json").write_text(
            json.dumps(quality, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        redacted_sample = build_redacted_sample(
            {
                table: records
                for table, records, _ in source_tables
            },
            approved_comment_keys=approved_comment_keys,
        )
        (temporary_directory / "redacted_sample.json").write_text(
            json.dumps(
                redacted_sample,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        updated_index = {
            table: set(keys)
            for table, keys in key_index.items()
        }
        for table, new_keys in new_keys_by_table.items():
            updated_index[table].update(new_keys)
        run_keys = {
            table: set(new_keys_by_table.get(table, set()))
            for table in _INDEX_TABLES
        }
        _write_key_index(
            temporary_directory / _RUN_KEYS_FILENAME,
            run_keys,
        )
        _write_key_index(temporary_index, updated_index)
        temporary_directory.replace(directory)
        try:
            temporary_index.replace(index_path)
        except OSError:
            # 根索引只是缓存；完整运行目录及其键清单已经原子发布。
            # 后续运行会扫描键清单重建缓存，不应把成功结果回滚。
            pass
    finally:
        if temporary_directory.exists():
            shutil.rmtree(temporary_directory)
        if temporary_index.exists():
            temporary_index.unlink()

    return tuple(directory / filename for filename in published_filenames)


__all__ = ["write_collection_result"]
