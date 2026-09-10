"""显式语言优先的中英文语言解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from translation_platform.config import SUPPORTED_LANGUAGES
from translation_platform.errors import ConfigurationFailure


class LanguageResolutionError(ConfigurationFailure, ValueError):
    """文本或语言参数无法得到有效语言对。"""


@dataclass(frozen=True)
class LanguageDecision:
    """最终源语言、目标语言及源语言是否来自推断。"""

    from_lang: str
    to_lang: str
    inferred_source: bool


def resolve_language_pair(
    text: str,
    from_lang: Optional[str] = None,
    to_lang: Optional[str] = None,
) -> LanguageDecision:
    """优先采用显式源语言，仅在缺省时检测文本。"""

    normalized_text = _normalize_text(text)
    _validate_optional_language("from_lang", from_lang)
    _validate_optional_language("to_lang", to_lang)

    inferred_source = from_lang is None
    source = detect_source_language(normalized_text) if inferred_source else from_lang
    target = to_lang if to_lang is not None else _opposite_language(source)
    if source == target:
        raise LanguageResolutionError("source and target languages must differ")
    return LanguageDecision(
        from_lang=source,
        to_lang=target,
        inferred_source=inferred_source,
    )


def detect_source_language(text: str) -> str:
    """按支持字符数判定源语言，同数时采用首个语言字符。"""

    normalized_text = _normalize_text(text)
    chinese_count = 0
    english_count = 0
    first_signal: Optional[str] = None

    for character in normalized_text:
        if _is_chinese(character):
            chinese_count += 1
            if first_signal is None:
                first_signal = "zh-CHS"
        elif _is_ascii_letter(character):
            english_count += 1
            if first_signal is None:
                first_signal = "en"
        elif character.isalpha():
            raise LanguageResolutionError(
                "text contains an unsupported script for automatic detection"
            )

    if chinese_count == 0 and english_count == 0:
        raise LanguageResolutionError(
            "text contains no supported language signal; specify from_lang"
        )
    if chinese_count > english_count:
        return "zh-CHS"
    if english_count > chinese_count:
        return "en"
    if first_signal is None:
        raise AssertionError("language signal count is inconsistent")
    return first_signal


def _normalize_text(text: object) -> str:
    if not isinstance(text, str) or not text.strip():
        raise LanguageResolutionError("text must not be empty")
    return text.strip()


def _validate_optional_language(name: str, language: Optional[str]) -> None:
    if language is not None and language not in SUPPORTED_LANGUAGES:
        raise LanguageResolutionError(f"unsupported language for {name}: {language}")


def _opposite_language(language: str) -> str:
    if language == "en":
        return "zh-CHS"
    if language == "zh-CHS":
        return "en"
    raise LanguageResolutionError(f"unsupported language: {language}")


def _is_ascii_letter(character: str) -> bool:
    return "A" <= character <= "Z" or "a" <= character <= "z"


def _is_chinese(character: str) -> bool:
    code_point = ord(character)
    return (
        0x3400 <= code_point <= 0x4DBF
        or 0x4E00 <= code_point <= 0x9FFF
        or 0xF900 <= code_point <= 0xFAFF
        or 0x20000 <= code_point <= 0x2FA1F
        or 0x30000 <= code_point <= 0x323AF
    )
