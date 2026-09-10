"""通用 AI 翻译请求的 JavaScript 签名封装。"""

from __future__ import annotations

import base64
import binascii
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

import execjs

from translation_platform.paths import resolve_project_path
from translation_platform.errors import SignatureTokenFailure


Clock = Callable[[], int]
Compiler = Callable[[str], Any]
CacheKey = Tuple[Path, Compiler]


class SignerError(SignatureTokenFailure, ValueError):
    """签名配置、输入或 JavaScript 执行失败时抛出。"""


@dataclass(frozen=True)
class SignatureResult:
    """一次签名使用的时间戳、原文和摘要。"""

    mystic_time: str
    payload: str
    signature: str


class JsSigner:
    """复用已编译 JavaScript 上下文的确定性签名器。"""

    _contexts: Dict[CacheKey, Any] = {}
    _contexts_lock = Lock()

    def __init__(
        self,
        js_path: Optional[Path] = None,
        clock: Optional[Clock] = None,
        compiler: Compiler = execjs.compile,
    ) -> None:
        self._js_path = (
            js_path.resolve()
            if js_path is not None
            else resolve_project_path("js", "sign.js").resolve()
        )
        self._clock = clock if clock is not None else _system_clock_millis
        self._context = self._get_or_compile_context(compiler)

    def sign(
        self,
        parameters: Mapping[str, object],
        ordered_fields: Sequence[str],
        signing_key: str,
    ) -> SignatureResult:
        """使用一次时钟读数构造有序原文并返回签名。"""

        fields = _validate_signing_inputs(parameters, ordered_fields, signing_key)
        mystic_time = self._read_clock()
        try:
            raw_result = self._context.call(
                "signOrderedParameters",
                dict(parameters),
                list(fields),
                signing_key,
                mystic_time,
            )
        except (execjs.Error, TypeError, ValueError) as exc:
            raise SignerError(f"JavaScript signing failed: {exc}") from exc
        return _parse_result(raw_result)

    def _get_or_compile_context(self, compiler: Compiler) -> Any:
        cache_key = (self._js_path, compiler)
        with self._contexts_lock:
            context = self._contexts.get(cache_key)
            if context is not None:
                return context
            try:
                source = self._js_path.read_text(encoding="utf-8")
                context = compiler(source)
            except OSError as exc:
                raise SignerError(f"unable to read JavaScript signer: {exc}") from exc
            except execjs.Error as exc:
                raise SignerError(f"unable to compile JavaScript signer: {exc}") from exc
            self._contexts[cache_key] = context
            return context

    def _read_clock(self) -> int:
        value = self._clock()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SignerError("clock must return a non-negative integer millisecond value")
        return value


def _system_clock_millis() -> int:
    return int(time.time() * 1000)


def _validate_signing_inputs(
    parameters: Mapping[str, object],
    ordered_fields: Sequence[str],
    signing_key: str,
) -> Tuple[str, ...]:
    if not isinstance(parameters, Mapping):
        raise SignerError("parameters must be a mapping")
    try:
        json.dumps(dict(parameters), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise SignerError("signing parameters must be JSON-compatible") from exc
    if not isinstance(signing_key, str) or not signing_key:
        raise SignerError("signing key must not be empty")
    try:
        fields = tuple(ordered_fields)
    except TypeError as exc:
        raise SignerError("ordered signing fields must be a sequence") from exc
    if not fields or any(not isinstance(field, str) or not field for field in fields):
        raise SignerError("ordered signing fields must be non-empty text")
    if len(set(fields)) != len(fields):
        raise SignerError("ordered signing fields must not contain duplicates")

    generated_fields = {"mysticTime", "key"}
    if not generated_fields.issubset(fields):
        raise SignerError(
            "ordered signing fields must include mysticTime and key"
        )
    for field in fields:
        if field not in generated_fields and field not in parameters:
            raise SignerError(f"missing signing parameter: {field}")
    return fields


def _parse_result(raw_result: object) -> SignatureResult:
    raw_result = _decode_ascii_transport(raw_result)
    if not isinstance(raw_result, dict):
        raise SignerError("JavaScript signer returned an invalid result")
    mystic_time = raw_result.get("mysticTime")
    payload = raw_result.get("payload")
    signature = raw_result.get("signature")
    if not all(isinstance(value, str) and value for value in (mystic_time, payload, signature)):
        raise SignerError("JavaScript signer returned incomplete fields")
    return SignatureResult(
        mystic_time=mystic_time,
        payload=payload,
        signature=signature,
    )


def _decode_ascii_transport(raw_result: object) -> object:
    if not isinstance(raw_result, dict):
        return raw_result
    if raw_result.get("transportEncoding") != "base64-json-v1":
        return raw_result
    encoded = raw_result.get("data")
    if not isinstance(encoded, str) or not encoded:
        raise SignerError("JavaScript 签名结果的 ASCII 信封无效")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        return json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SignerError("JavaScript 签名结果的 ASCII 信封无效") from None
