"""批量翻译任务的缓存键与源单元格去重。"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Tuple

from translation_platform.batch_input import CellPosition, SourceCell


@dataclass(frozen=True)
class BatchTask:
    """共享缓存键的一项翻译任务及其全部源单元格位置。"""

    cache_key: str
    source_lang: str
    target_lang: str
    normalized_text: str
    positions: Tuple[CellPosition, ...]


def normalize_cache_text(text: str) -> str:
    """将文本规范为 NFKC 形式，并合并所有首尾和连续空白。"""

    return " ".join(unicodedata.normalize("NFKC", text).split())


def build_cache_key(source_lang: str, target_lang: str, text: str) -> str:
    """以长度前缀编码语言方向和规范化文本，生成不含原文的 SHA-256 摘要。"""

    normalized_text = normalize_cache_text(text)
    encoded_fields = tuple(
        field.encode("utf-8") for field in (source_lang, target_lang, normalized_text)
    )
    payload = b"".join(
        len(field).to_bytes(8, byteorder="big") + field for field in encoded_fields
    )
    return hashlib.sha256(payload).hexdigest()


def deduplicate_cells(
    cells: Iterable[SourceCell], source_lang: str, target_lang: str
) -> Tuple[BatchTask, ...]:
    """按缓存键合并等价单元格，并按首次出现顺序返回任务。"""

    grouped_positions = {}
    normalized_texts = {}
    for cell in cells:
        normalized_text = normalize_cache_text(cell.text)
        if not normalized_text:
            continue
        cache_key = build_cache_key(source_lang, target_lang, cell.text)
        if cache_key not in grouped_positions:
            grouped_positions[cache_key] = []
            normalized_texts[cache_key] = normalized_text
        grouped_positions[cache_key].append(cell.position)

    return tuple(
        BatchTask(
            cache_key=cache_key,
            source_lang=source_lang,
            target_lang=target_lang,
            normalized_text=normalized_texts[cache_key],
            positions=tuple(positions),
        )
        for cache_key, positions in grouped_positions.items()
    )
