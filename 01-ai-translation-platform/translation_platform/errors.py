"""翻译平台统一错误层级和结构化分类。"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class ErrorType(str, Enum):
    """供请求、批处理和运行摘要共用的错误分类。"""

    CONFIGURATION = "configuration"
    SIGNATURE_TOKEN = "signature_token"
    NETWORK_CONNECTION = "network_connection"
    TIMEOUT = "timeout"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    RATE_LIMIT = "rate_limit"
    RESPONSE_FORMAT = "response_format"
    EMPTY_RESULT = "empty_result"


class TranslationPlatformError(Exception):
    """所有可分类平台错误的公共基类。"""

    error_type: ErrorType


class ConfigurationFailure(TranslationPlatformError):
    """配置、输入或调用参数不合法。"""

    error_type = ErrorType.CONFIGURATION


class SignatureTokenFailure(TranslationPlatformError):
    """签名、token 获取或鉴权失败。"""

    error_type = ErrorType.SIGNATURE_TOKEN


class NetworkConnectionError(TranslationPlatformError):
    """连接建立、中断或底层网络传输失败。"""

    error_type = ErrorType.NETWORK_CONNECTION


class RequestTimeoutError(TranslationPlatformError):
    """连接或读取超过配置时限。"""

    error_type = ErrorType.TIMEOUT


class HttpStatusError(TranslationPlatformError):
    """HTTP 状态检查失败的公共基类。"""

    error_type = ErrorType.RESPONSE_FORMAT

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class Http4xxError(HttpStatusError):
    """除鉴权和限频外的 HTTP 4xx。"""

    error_type = ErrorType.HTTP_4XX


class AccessRestrictionError(Http4xxError):
    """HTTP 403 或等价访问限制，不能通过刷新 token 恢复。"""


class Http5xxError(HttpStatusError):
    """HTTP 5xx 服务端错误。"""

    error_type = ErrorType.HTTP_5XX


class RateLimitError(Http4xxError):
    """HTTP 429 或等价限频错误。"""

    error_type = ErrorType.RATE_LIMIT

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = 429,
        retry_after_raw: Optional[str] = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after_raw = retry_after_raw


class ResponseFormatError(TranslationPlatformError):
    """JSON、SSE 或响应字段结构无效。"""

    error_type = ErrorType.RESPONSE_FORMAT


class EmptyTranslationError(TranslationPlatformError):
    """响应流程成功但没有可用翻译文本。"""

    error_type = ErrorType.EMPTY_RESULT
