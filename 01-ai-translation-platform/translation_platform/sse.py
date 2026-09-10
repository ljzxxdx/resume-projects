"""逐行解析 Server-Sent Events 翻译响应。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

from translation_platform.errors import (
    EmptyTranslationError,
    ErrorType,
    ResponseFormatError,
)


class SseError(ResponseFormatError, ValueError):
    """SSE 响应解析错误基类。"""

    error_type = ErrorType.RESPONSE_FORMAT


class SseDecodeError(SseError):
    """事件行无法按 UTF-8 解码。"""


class SseJsonError(SseError):
    """data 事件不是有效 JSON 对象。"""


class SseContentError(SseError):
    """响应没有可用文本或内容字段类型错误。"""


class SseEmptyContentError(EmptyTranslationError, SseContentError):
    """SSE 正常结束但没有任何翻译文本。"""


class SseServerError(SseError):
    """服务端通过 SSE 事件返回业务错误。"""

    def __init__(self, message: str, server_code: Optional[str] = None) -> None:
        super().__init__(message)
        self.server_code = server_code


@dataclass(frozen=True)
class SseParseResult:
    """合并后的响应文本及事件终止信息。"""

    content: str
    event_count: int
    completed: bool


class SseParser:
    """按 SSE 行和事件边界解析 JSON 内容分片。"""

    def __init__(
        self,
        content_path: Sequence[str] = ("content",),
        done_marker: str = "[DONE]",
    ) -> None:
        self._content_path = _validate_content_path(content_path)
        if not isinstance(done_marker, str) or not done_marker:
            raise SseContentError("done_marker must not be empty")
        self._done_marker = done_marker

    def parse_response(self, response: Any) -> SseParseResult:
        """解析具备 ``iter_lines`` 的流式 HTTP 响应。"""

        iter_lines = getattr(response, "iter_lines", None)
        if not callable(iter_lines):
            raise SseDecodeError("response must provide iter_lines")
        return self.parse_lines(iter_lines(decode_unicode=False))

    def parse_lines(self, lines: Iterable[object]) -> SseParseResult:
        """逐行消费 SSE；空行提交事件，输入结束时提交剩余事件。"""

        if isinstance(lines, (str, bytes)) or not isinstance(lines, Iterable):
            raise SseDecodeError("SSE input must be an iterable of lines")

        data_lines: List[str] = []
        chunks: List[str] = []
        event_count = 0
        completed = False

        for raw_line in lines:
            line = _decode_line(raw_line)
            if line == "":
                done, chunk, counted = self._dispatch_event(
                    data_lines,
                    event_count + 1,
                )
                data_lines.clear()
                event_count += counted
                if chunk is not None:
                    chunks.append(chunk)
                if done:
                    completed = True
                    break
                continue
            if line.startswith(":"):
                continue

            field, separator, value = line.partition(":")
            if field != "data":
                continue
            if separator and value.startswith(" "):
                value = value[1:]
            data_lines.append(value)

        if not completed and data_lines:
            done, chunk, counted = self._dispatch_event(
                data_lines,
                event_count + 1,
            )
            event_count += counted
            if chunk is not None:
                chunks.append(chunk)
            completed = done

        content = "".join(chunks)
        if not content:
            raise SseEmptyContentError("SSE response contains no content")
        return SseParseResult(
            content=content,
            event_count=event_count,
            completed=completed,
        )

    def _dispatch_event(
        self,
        data_lines: Sequence[str],
        event_number: int,
    ) -> Tuple[bool, Optional[str], int]:
        if not data_lines:
            return False, None, 0
        data = "\n".join(data_lines)
        if data == self._done_marker:
            return True, None, 0
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SseJsonError(
                f"invalid JSON in SSE event {event_number}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise SseJsonError(
                f"SSE event {event_number} must contain a JSON object"
            )
        _raise_server_error(payload)
        return False, _extract_content(payload, self._content_path), 1


def _decode_line(raw_line: object) -> str:
    if isinstance(raw_line, bytes):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SseDecodeError("SSE line is not valid UTF-8") from exc
    elif isinstance(raw_line, str):
        line = raw_line
    else:
        raise SseDecodeError("SSE lines must be text or bytes")
    return line.rstrip("\r\n")


def _validate_content_path(content_path: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(content_path, (str, bytes)):
        raise SseContentError("content_path must be a field sequence")
    try:
        path = tuple(content_path)
    except TypeError as exc:
        raise SseContentError("content_path must be a field sequence") from exc
    if not path or any(not isinstance(field, str) or not field for field in path):
        raise SseContentError("content_path must contain non-empty field names")
    return path


def _extract_content(
    payload: Mapping[str, object],
    path: Sequence[str],
) -> Optional[str]:
    value: object = payload
    for field in path:
        if not isinstance(value, Mapping) or field not in value:
            return None
        value = value[field]
    if value is None:
        return None
    if not isinstance(value, str):
        raise SseContentError("SSE content field must be text")
    return value


def _raise_server_error(payload: Mapping[str, object]) -> None:
    error = payload.get("error")
    if error is None and payload.get("type") != "error":
        return

    server_code: Optional[str] = None
    message = "server reported an SSE error"
    if isinstance(error, str) and error:
        message = error
    elif isinstance(error, Mapping):
        raw_message = error.get("message")
        raw_code = error.get("code")
        if isinstance(raw_message, str) and raw_message:
            message = raw_message
        if isinstance(raw_code, str) and raw_code:
            server_code = raw_code
    else:
        raw_message = payload.get("message")
        raw_code = payload.get("code")
        if isinstance(raw_message, str) and raw_message:
            message = raw_message
        if isinstance(raw_code, str) and raw_code:
            server_code = raw_code
    raise SseServerError(message, server_code=server_code)
