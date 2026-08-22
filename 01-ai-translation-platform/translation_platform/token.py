"""token 的按需获取、缓存和单次刷新策略。"""

from __future__ import annotations

from threading import RLock
from typing import Callable, Optional, TypeVar

from translation_platform.errors import (
    ConfigurationFailure,
    ErrorType,
    SignatureTokenFailure,
    TranslationPlatformError,
)


ResultT = TypeVar("ResultT")
TokenProvider = Callable[[], str]
TokenOperation = Callable[[str], ResultT]


class TokenError(SignatureTokenFailure, RuntimeError):
    """token 生命周期错误基类。"""


class TokenConfigurationError(ConfigurationFailure, TokenError):
    """token 管理器配置无效。"""


class TokenAcquisitionError(TokenError):
    """token 获取失败或返回值无效。"""


class AuthenticationFailure(TokenError):
    """带 token 的操作明确返回鉴权失败。"""

    def __init__(self, message: str, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TokenRefreshExhausted(TokenError):
    """一次刷新机会用尽后仍无法完成操作。"""

    error_type = ErrorType.SIGNATURE_TOKEN

    def __init__(
        self,
        message: str,
        operation_attempts: int,
        status_code: Optional[int] = None,
        proxy_id_summary: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.operation_attempts = operation_attempts
        self.status_code = status_code
        self.proxy_id_summary = proxy_id_summary


class TokenManager:
    """线程安全地缓存 token，并仅对鉴权失败刷新一次。"""

    def __init__(self, provider: TokenProvider) -> None:
        if not callable(provider):
            raise TokenConfigurationError("token provider must be callable")
        self._provider = provider
        self._token: Optional[str] = None
        self._lock = RLock()

    def get_token(self) -> str:
        """首次使用时获取 token，后续返回缓存值。"""

        with self._lock:
            if self._token is None:
                self._token = self._load_token()
            return self._token

    def execute_with_token(self, operation: TokenOperation[ResultT]) -> ResultT:
        """执行带 token 的操作，鉴权失败时最多刷新并重试一次。"""

        if not callable(operation):
            raise TokenConfigurationError("token operation must be callable")
        initial_token = self.get_token()
        try:
            return operation(initial_token)
        except AuthenticationFailure as exc:
            initial_failure = exc

        try:
            refreshed_token = self._refresh_token(initial_token)
        except TokenAcquisitionError as exc:
            raise TokenRefreshExhausted(
                "token refresh failed",
                operation_attempts=1,
                status_code=initial_failure.status_code,
                proxy_id_summary=getattr(
                    initial_failure,
                    "proxy_id_summary",
                    None,
                ),
            ) from exc

        try:
            return operation(refreshed_token)
        except AuthenticationFailure as exc:
            self._discard_if_current(refreshed_token)
            raise TokenRefreshExhausted(
                "authentication failed after one refresh",
                operation_attempts=2,
                status_code=(
                    exc.status_code
                    if exc.status_code is not None
                    else initial_failure.status_code
                ),
                proxy_id_summary=getattr(
                    exc,
                    "proxy_id_summary",
                    getattr(initial_failure, "proxy_id_summary", None),
                ),
            ) from exc

    def _refresh_token(self, stale_token: str) -> str:
        with self._lock:
            # 并发请求已完成刷新时直接复用新值，避免重复访问 token 端点。
            if self._token is not None and self._token != stale_token:
                return self._token
            self._token = None
            self._token = self._load_token()
            return self._token

    def _load_token(self) -> str:
        try:
            token = self._provider()
        except TranslationPlatformError:
            raise
        except Exception as exc:
            raise TokenAcquisitionError("unable to acquire token") from exc
        if not isinstance(token, str) or not token.strip():
            raise TokenAcquisitionError("token provider returned an empty token")
        return token.strip()

    def _discard_if_current(self, token: str) -> None:
        with self._lock:
            if self._token == token:
                self._token = None
