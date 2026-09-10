"""翻译平台的会话传输和单次业务请求边界。"""

from __future__ import annotations

import math
import random
import time
from collections.abc import Mapping
from collections.abc import MutableMapping
from contextlib import nullcontext
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, Optional, Sequence, Tuple, TypeVar

import requests

from translation_platform.errors import (
    AccessRestrictionError,
    ConfigurationFailure,
    Http4xxError,
    Http5xxError,
    HttpStatusError,
    NetworkConnectionError,
    RateLimitError,
    RequestTimeoutError,
    ResponseFormatError,
    TranslationPlatformError,
)
from translation_platform.config import RuntimeConfig
from translation_platform.pacing import (
    GlobalRateLimiter,
    QueueTaskResult,
    TaskQueueRunner,
)
from translation_platform.proxy import (
    DEFAULT_ENVIRONMENT_VARIABLE,
    ProxyManager,
    ProxySettings,
)
from translation_platform.retry import RetryExecutor, RetryOutcome, RetryPolicy
from translation_platform.access import (
    AccessFailure,
    AccessFallbackController,
    AccessOutcome,
)

from translation_platform.protocol import (
    EndpointKind,
    EndpointSigningProfile,
    SignedRequest,
    SignedRequestBuilder,
)
from translation_platform.token import (
    AuthenticationFailure,
    TokenAcquisitionError,
    TokenManager,
)
from translation_platform.sse import SseParseResult, SseParser


DEFAULT_HEADERS = MappingProxyType(
    {
        "Accept": "application/json, text/event-stream",
        "User-Agent": "ai-translation-platform/0.1",
    }
)


ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")


class HttpClientError(TranslationPlatformError, RuntimeError):
    """会话传输层错误基类。"""


class HttpClientConfigurationError(ConfigurationFailure, HttpClientError):
    """会话配置无效。"""


class ClientClosedError(ConfigurationFailure, HttpClientError):
    """会话关闭后仍尝试发起请求。"""


class JsonResponseError(ResponseFormatError, HttpClientError):
    """响应不是有效 JSON 对象。"""


class BusinessPostError(NetworkConnectionError, HttpClientError):
    """业务 POST 未发送或单次发送失败。"""


class SessionHttpClient:
    """统一管理 Session、请求头、超时、状态检查和关闭行为。"""

    def __init__(
        self,
        session: Optional[Any] = None,
        rate_limiter: Optional[GlobalRateLimiter] = None,
        proxy_manager: Optional[ProxyManager] = None,
        headers: Optional[Mapping[str, str]] = None,
        connect_timeout: float = 3.05,
        read_timeout: float = 15.0,
    ) -> None:
        self._connect_timeout = _validate_timeout("connect_timeout", connect_timeout)
        self._read_timeout = _validate_timeout("read_timeout", read_timeout)
        if not isinstance(rate_limiter, GlobalRateLimiter):
            raise HttpClientConfigurationError("GlobalRateLimiter is required")
        self._rate_limiter = rate_limiter
        self._session = requests.Session() if session is None else session
        if not callable(getattr(self._session, "post", None)):
            raise HttpClientConfigurationError("session must provide a post method")
        if not callable(getattr(self._session, "close", None)):
            raise HttpClientConfigurationError("session must provide a close method")
        if not hasattr(self._session, "headers"):
            raise HttpClientConfigurationError("session must provide headers")
        if proxy_manager is not None:
            if (
                not isinstance(proxy_manager, ProxyManager)
                or not proxy_manager.is_bound_to(self._session)
            ):
                raise HttpClientConfigurationError(
                    "ProxyManager must be bound to the same session"
                )
        else:
            session_proxies = getattr(self._session, "proxies", None)
            if isinstance(session_proxies, MutableMapping):
                session_proxies.clear()
            setattr(self._session, "trust_env", False)
        self._proxy_manager = proxy_manager

        merged_headers: Dict[str, str] = dict(DEFAULT_HEADERS)
        if headers is not None:
            merged_headers.update(_copy_headers(headers))
        self._session.headers.update(merged_headers)
        self._closed = False

    @property
    def session(self) -> Any:
        """返回当前持有的 Session，便于诊断和受控扩展。"""

        return self._session

    @property
    def proxy_manager(self) -> Optional[ProxyManager]:
        """返回只含脱敏标识查询能力的代理管理器。"""

        return self._proxy_manager

    def post(
        self,
        url: str,
        params: Mapping[str, str],
        stream: bool = False,
    ) -> Any:
        """执行一次 POST，并在返回前检查 HTTP 状态。"""

        return self._request(
            "post",
            url=url,
            parameters=params,
            stream=stream,
            parameter_location="query",
        )

    def post_form(
        self,
        url: str,
        data: Mapping[str, str],
        stream: bool = False,
    ) -> Any:
        """执行一次表单 POST，并复用相同传输边界。"""

        return self._request(
            "post",
            url=url,
            parameters=data,
            stream=stream,
            parameter_location="form",
        )

    def get(
        self,
        url: str,
        params: Mapping[str, str],
        stream: bool = False,
    ) -> Any:
        """执行一次 GET，并复用相同限速、超时和状态检查边界。"""

        return self._request(
            "get",
            url=url,
            parameters=params,
            stream=stream,
            parameter_location="query",
        )

    def _request(
        self,
        method_name: str,
        url: str,
        parameters: Mapping[str, str],
        stream: bool,
        parameter_location: str,
    ) -> Any:
        request_method = getattr(self._session, method_name, None)
        if not callable(request_method):
            raise HttpClientConfigurationError(
                "Session 不支持所需的 HTTP " + method_name.upper() + " 方法"
            )

        self._ensure_open()
        self._rate_limiter.wait()
        request_scope = (
            self._proxy_manager.request_scope()
            if self._proxy_manager is not None
            else nullcontext(None)
        )
        with request_scope as proxy_id:
            try:
                parameter_key = "data" if parameter_location == "form" else "params"
                request_arguments: Dict[str, object] = {
                    parameter_key: dict(parameters),
                    "timeout": (self._connect_timeout, self._read_timeout),
                    "stream": stream,
                }
                response = request_method(url, **request_arguments)
            except requests.Timeout as exc:
                error = RequestTimeoutError("HTTP request timed out")
                if self._proxy_manager is not None and self._proxy_manager.enabled:
                    raise error from None
                raise error from exc
            except requests.ConnectionError as exc:
                error = NetworkConnectionError("HTTP connection failed")
                proxy_switched = False
                if self._proxy_manager is not None:
                    proxy_switched = self._proxy_manager.handle_connection_failure(
                        error,
                        proxy_id=proxy_id,
                    )
                    setattr(error, "proxy_switches", 1 if proxy_switched else 0)
                    if self._proxy_manager.enabled:
                        raise error from None
                raise error from exc
            try:
                response.raise_for_status()
            except Exception as exc:
                status_code = getattr(response, "status_code", None)
                _close_response(response)
                if status_code == 401:
                    raise _attach_proxy_id(
                        AuthenticationFailure(
                            f"HTTP authentication failed with status {status_code}",
                            status_code=status_code,
                        ),
                        proxy_id,
                    ) from exc
                if status_code == 403:
                    raise _attach_proxy_id(
                        AccessRestrictionError(
                            "HTTP access restricted with status 403",
                            status_code=status_code,
                        ),
                        proxy_id,
                    ) from exc
                if status_code == 429:
                    raise _attach_proxy_id(
                        RateLimitError(
                            "HTTP rate limit exceeded",
                            status_code=status_code,
                            retry_after_raw=getattr(response, "headers", {}).get(
                                "Retry-After"
                            ),
                        ),
                        proxy_id,
                    ) from exc
                if isinstance(status_code, int) and 400 <= status_code < 500:
                    raise _attach_proxy_id(
                        Http4xxError(
                            "HTTP client error",
                            status_code=status_code,
                        ),
                        proxy_id,
                    ) from exc
                if isinstance(status_code, int) and 500 <= status_code < 600:
                    raise _attach_proxy_id(
                        Http5xxError(
                            "HTTP server error",
                            status_code=status_code,
                        ),
                        proxy_id,
                    ) from exc
                raise _attach_proxy_id(
                    HttpStatusError(
                        "HTTP status check failed",
                        status_code=(
                            status_code if isinstance(status_code, int) else None
                        ),
                    ),
                    proxy_id,
                ) from exc
            if self._proxy_manager is not None:
                self._proxy_manager.mark_success(proxy_id=proxy_id)
            return response

    def post_json(
        self,
        url: str,
        params: Mapping[str, str],
    ) -> Mapping[str, object]:
        """执行一次 POST，并要求响应顶层为 JSON 对象。"""

        return self._json_response(self.post(url=url, params=params, stream=False))

    def get_json(
        self,
        url: str,
        params: Mapping[str, str],
    ) -> Mapping[str, object]:
        """执行一次 GET，并要求响应顶层为 JSON 对象。"""

        return self._json_response(self.get(url=url, params=params, stream=False))

    @staticmethod
    def _json_response(response: Any) -> Mapping[str, object]:
        try:
            payload = response.json()
        except Exception as exc:
            raise JsonResponseError("response contains invalid JSON") from exc
        if not isinstance(payload, Mapping):
            raise JsonResponseError("response JSON must be a JSON object")
        return MappingProxyType(dict(payload))

    def close(self) -> None:
        """显式关闭 Session；重复关闭不会再次操作底层资源。"""

        if self._closed:
            return
        self._session.close()
        self._closed = True

    def __enter__(self) -> "SessionHttpClient":
        self._ensure_open()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ClientClosedError("HTTP client is closed")


class RequestRuntime:
    """由不可变配置创建共享限速传输和有限工作队列。"""

    def __init__(
        self,
        config: RuntimeConfig,
        monotonic: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        proxy_settings: Optional[ProxySettings] = None,
    ) -> None:
        if not isinstance(config, RuntimeConfig):
            raise HttpClientConfigurationError("RuntimeConfig is required")
        resolved_proxy_settings = (
            ProxySettings() if proxy_settings is None else proxy_settings
        )
        if not isinstance(resolved_proxy_settings, ProxySettings):
            raise HttpClientConfigurationError("ProxySettings is required")
        if resolved_proxy_settings.enabled != config.use_proxy:
            raise HttpClientConfigurationError(
                "runtime config and proxy settings must agree"
            )
        limiter_options: Dict[str, object] = {}
        if monotonic is not None:
            limiter_options["monotonic"] = monotonic
        if sleep is not None:
            limiter_options["sleep"] = sleep
        self._config = config
        self._monotonic = monotonic
        self._sleep = time.sleep if sleep is None else sleep
        self._proxy_settings = resolved_proxy_settings
        self._rate_limiter = GlobalRateLimiter(
            config.min_interval_seconds,
            **limiter_options,
        )

    @classmethod
    def from_proxy_sources(
        cls,
        config: RuntimeConfig,
        *,
        environ: Optional[Mapping[str, str]] = None,
        private_config: Optional[Mapping[str, object]] = None,
        max_proxy_switches: int = 1,
        proxy_cooldown_seconds: float = 30.0,
        access_status_fallback_enabled: bool = False,
        monotonic: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> "RequestRuntime":
        """按运行配置从专用环境变量或私有配置装载代理。"""

        variable_name = config.proxy_reference or DEFAULT_ENVIRONMENT_VARIABLE
        settings = ProxySettings.from_sources(
            enabled=config.use_proxy,
            environ=environ,
            private_config=private_config,
            environment_variable=variable_name,
            max_switches=max_proxy_switches,
            cooldown_seconds=proxy_cooldown_seconds,
            access_status_fallback_enabled=access_status_fallback_enabled,
        )
        return cls(
            config,
            monotonic=monotonic,
            sleep=sleep,
            proxy_settings=settings,
        )

    def create_http_client(
        self,
        session: Optional[Any] = None,
        headers: Optional[Mapping[str, str]] = None,
    ) -> SessionHttpClient:
        """创建强制共享同一全局限速器的 HTTP 客户端。"""

        owned_session = requests.Session() if session is None else session
        proxy_manager = ProxyManager(
            self._proxy_settings,
            session=owned_session,
            monotonic=(
                self._monotonic
                if self._monotonic is not None
                else time.monotonic
            ),
        )
        return SessionHttpClient(
            session=owned_session,
            rate_limiter=self._rate_limiter,
            proxy_manager=proxy_manager,
            headers=headers,
            connect_timeout=min(3.05, self._config.request_timeout_seconds),
            read_timeout=self._config.request_timeout_seconds,
        )

    def run(
        self,
        items: Iterable[ItemT],
        handler: Callable[[ItemT], ResultT],
    ) -> Tuple[QueueTaskResult[ResultT], ...]:
        """按配置的至多两个工作线程处理有限任务集合。"""

        return TaskQueueRunner(
            workers=self._config.workers,
            handler=handler,
        ).run(items)

    def execute_with_retry(
        self,
        operation: Callable[[], ResultT],
        random_source: Callable[[], float] = random.random,
    ) -> RetryOutcome[ResultT]:
        """按运行配置对真实操作执行有限重试和指数退避。"""

        return self._create_retry_executor(random_source).execute(operation)

    def create_stable_translation_client(
        self,
        authenticated_client: "AuthenticatedTranslationClient",
        access_controller: AccessFallbackController,
        random_source: Callable[[], float] = random.random,
    ) -> "StableTranslationClient":
        """创建固定组合顺序的生产翻译客户端，避免失败结果被误判。"""

        return StableTranslationClient(
            authenticated_client=authenticated_client,
            access_controller=access_controller,
            retry_executor=self._create_retry_executor(random_source),
        )

    def _create_retry_executor(
        self,
        random_source: Callable[[], float],
    ) -> RetryExecutor:
        return RetryExecutor(
            policy=RetryPolicy(max_retries=self._config.max_retries),
            sleep=self._sleep,
            random_source=random_source,
        )

    def execute_managed(
        self,
        client: SessionHttpClient,
        operation: Callable[[], ResultT],
        save_checkpoint: Callable[[object], None],
        write_summary: Callable[[object], None],
    ) -> ResultT:
        """在三种退出路径下统一关闭客户端、保存断点并写摘要。"""

        from translation_platform.lifecycle import RunLifecycle

        if not isinstance(client, SessionHttpClient):
            raise HttpClientConfigurationError("SessionHttpClient is required")
        return RunLifecycle(
            session_owner=client,
            save_checkpoint=save_checkpoint,
            write_summary=write_summary,
        ).execute(operation)


class BusinessRequestClient:
    """每次调用只执行一次业务 POST，不在内部隐式重发。"""

    def __init__(
        self,
        transport: SessionHttpClient,
        business_url: str,
        parameter_location: str = "query",
    ) -> None:
        if not isinstance(transport, SessionHttpClient):
            raise BusinessPostError("SessionHttpClient transport is required")
        if not isinstance(business_url, str) or not business_url.strip():
            raise BusinessPostError("business_url must not be empty")
        if parameter_location not in {"query", "form"}:
            raise BusinessPostError("parameter_location 只支持 query 或 form")
        self._transport = transport
        self._business_url = business_url
        self._parameter_location = parameter_location

    def post_translation(self, request: SignedRequest) -> Any:
        """发送一次 chat 业务请求并返回已检查状态的流式响应。"""

        if not isinstance(request, SignedRequest):
            raise BusinessPostError("signed request is required")
        if request.endpoint is not EndpointKind.CHAT:
            raise BusinessPostError("a signed chat request is required")

        try:
            if self._parameter_location == "form":
                return self._transport.post_form(
                    self._business_url,
                    data=request.parameters,
                    stream=True,
                )
            return self._transport.post(
                self._business_url,
                params=request.parameters,
                stream=True,
            )
        except TranslationPlatformError:
            raise
        except Exception as exc:
            # 此层不重试，异常只对应上方唯一一次业务网络调用。
            raise BusinessPostError("business POST failed") from exc


class JsonTokenProvider:
    """通过 secret JSON 端点获取并校验 token。"""

    def __init__(
        self,
        transport: SessionHttpClient,
        request_builder: SignedRequestBuilder,
        profile: EndpointSigningProfile,
        secret_url: str,
        yduuid: str,
        token_field: str = "token",
        request_method: str = "POST",
        token_path: Optional[Sequence[str]] = None,
    ) -> None:
        if profile.endpoint is not EndpointKind.SECRET:
            raise HttpClientConfigurationError("secret profile is required")
        self._transport = transport
        self._request_builder = request_builder
        self._profile = profile
        self._secret_url = _require_text("secret_url", secret_url)
        self._yduuid = _require_text("yduuid", yduuid)
        normalized_method = _require_text("request_method", request_method).upper()
        if normalized_method not in {"GET", "POST"}:
            raise HttpClientConfigurationError("request_method 只支持 GET 或 POST")
        self._request_method = normalized_method
        if token_path is None:
            self._token_path = (_require_text("token_field", token_field),)
        else:
            if isinstance(token_path, (str, bytes)):
                raise HttpClientConfigurationError("token_path 必须是字段序列")
            try:
                normalized_path = tuple(token_path)
            except TypeError:
                raise HttpClientConfigurationError("token_path 必须是字段序列") from None
            if not normalized_path or any(
                not isinstance(field, str) or not field.strip()
                for field in normalized_path
            ):
                raise HttpClientConfigurationError("token_path 必须包含非空字段名")
            self._token_path = normalized_path

    def __call__(self) -> str:
        request = self._request_builder.build_secret(
            profile=self._profile,
            yduuid=self._yduuid,
        )
        if self._request_method == "GET":
            payload = self._transport.get_json(
                url=self._secret_url,
                params=request.parameters,
            )
        else:
            payload = self._transport.post_json(
                url=self._secret_url,
                params=request.parameters,
            )
        token: object = payload
        for field in self._token_path:
            if not isinstance(token, Mapping):
                token = None
                break
            token = token.get(field)
        if not isinstance(token, str) or not token.strip():
            raise TokenAcquisitionError("secret 响应不含有效 token")
        return token.strip()


class AuthenticatedTranslationClient:
    """把 token 生命周期、chat 签名和单次业务 POST 组成有限流程。"""

    def __init__(
        self,
        token_manager: TokenManager,
        request_builder: SignedRequestBuilder,
        chat_profile: EndpointSigningProfile,
        business_client: BusinessRequestClient,
        yduuid: str,
        sse_parser: SseParser,
    ) -> None:
        if chat_profile.endpoint is not EndpointKind.CHAT:
            raise HttpClientConfigurationError("chat profile is required")
        self._token_manager = token_manager
        self._request_builder = request_builder
        self._chat_profile = chat_profile
        self._business_client = business_client
        self._yduuid = _require_text("yduuid", yduuid)
        if not isinstance(sse_parser, SseParser):
            raise HttpClientConfigurationError("SseParser is required")
        self._sse_parser = sse_parser

    def post_translation(
        self,
        dynamic_parameters: Mapping[str, str],
    ) -> SseParseResult:
        """发送并解析 SSE；仅在 401 时刷新 token 后重试一次。"""

        def operation(token: str) -> SseParseResult:
            request = self._request_builder.build_chat(
                profile=self._chat_profile,
                token=token,
                yduuid=self._yduuid,
                dynamic_parameters=dynamic_parameters,
            )
            response = self._business_client.post_translation(request)
            try:
                return self._sse_parser.parse_response(response)
            finally:
                # 提前遇到结束标记或解析异常时也归还 Session 连接。
                _close_response(response)

        return self._token_manager.execute_with_token(operation)


class AccessControlledTranslationClient:
    """把认证翻译链路接入 Retry-After 和有限代理备用策略。"""

    def __init__(
        self,
        authenticated_client: AuthenticatedTranslationClient,
        access_controller: AccessFallbackController,
    ) -> None:
        if not isinstance(authenticated_client, AuthenticatedTranslationClient):
            raise HttpClientConfigurationError(
                "AuthenticatedTranslationClient is required"
            )
        if not isinstance(access_controller, AccessFallbackController):
            raise HttpClientConfigurationError("AccessFallbackController is required")
        self._authenticated_client = authenticated_client
        self._access_controller = access_controller

    def post_translation(
        self,
        dynamic_parameters: Mapping[str, str],
    ) -> AccessOutcome[SseParseResult]:
        """执行真实认证翻译链，并返回访问策略的结构化结果。"""

        return self._access_controller.execute(
            lambda: self._authenticated_client.post_translation(
                dynamic_parameters
            )
        )


@dataclass(frozen=True)
class StableTranslationOutcome:
    """重试与访问备用策略组合后的唯一结构化结果。"""

    value: Optional[SseParseResult]
    failure: Optional[AccessFailure]
    request_attempts: int
    access_attempts: int
    proxy_switches: int
    retry_proxy_switches: int
    access_proxy_switches: int
    retry_delays: Tuple[float, ...]

    @property
    def succeeded(self) -> bool:
        return self.failure is None


class StableTranslationClient:
    """先做可恢复请求重试，再处理访问限制和有限代理备用。"""

    def __init__(
        self,
        authenticated_client: AuthenticatedTranslationClient,
        access_controller: AccessFallbackController,
        retry_executor: RetryExecutor,
    ) -> None:
        if not isinstance(authenticated_client, AuthenticatedTranslationClient):
            raise HttpClientConfigurationError(
                "AuthenticatedTranslationClient is required"
            )
        if not isinstance(access_controller, AccessFallbackController):
            raise HttpClientConfigurationError("AccessFallbackController is required")
        if not isinstance(retry_executor, RetryExecutor):
            raise HttpClientConfigurationError("RetryExecutor is required")
        self._authenticated_client = authenticated_client
        self._access_controller = access_controller
        self._retry_executor = retry_executor

    def post_translation(
        self,
        dynamic_parameters: Mapping[str, str],
    ) -> StableTranslationOutcome:
        """执行无歧义的有限重试、Retry-After 和代理备用组合。"""

        request_attempts = 0
        retry_proxy_switches = 0
        retry_delays = []

        def access_operation() -> SseParseResult:
            nonlocal request_attempts, retry_proxy_switches
            retry_outcome = self._retry_executor.execute(
                lambda: self._authenticated_client.post_translation(
                    dynamic_parameters
                )
            )
            request_attempts += retry_outcome.attempts
            retry_proxy_switches += retry_outcome.proxy_switches
            retry_delays.extend(retry_outcome.retry_delays)
            if retry_outcome.failure is not None:
                raise retry_outcome.failure.last_error
            if retry_outcome.value is None:
                raise ResponseFormatError(
                    "retry operation succeeded without a translation result"
                )
            return retry_outcome.value

        access_outcome = self._access_controller.execute(access_operation)
        return StableTranslationOutcome(
            value=access_outcome.value,
            failure=access_outcome.failure,
            request_attempts=request_attempts,
            access_attempts=access_outcome.attempts,
            proxy_switches=(
                retry_proxy_switches + access_outcome.proxy_switches
            ),
            retry_proxy_switches=retry_proxy_switches,
            access_proxy_switches=access_outcome.proxy_switches,
            retry_delays=tuple(retry_delays),
        )


def _validate_timeout(name: str, value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise HttpClientConfigurationError(f"{name} must be a positive finite number")
    return float(value)


def _copy_headers(headers: Mapping[str, str]) -> Dict[str, str]:
    if not isinstance(headers, Mapping):
        raise HttpClientConfigurationError("headers must be a mapping")
    copied = dict(headers)
    if any(
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(value, str)
        or not value.strip()
        for name, value in copied.items()
    ):
        raise HttpClientConfigurationError("headers must contain non-empty text")
    return copied


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HttpClientConfigurationError(f"{name} must not be empty")
    return value.strip()


def _close_response(response: object) -> None:
    close = getattr(response, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        # 关闭属于清理动作，不能覆盖原始 HTTP 或 SSE 异常。
        return


def _attach_proxy_id(
    error: TranslationPlatformError,
    proxy_id: object,
) -> TranslationPlatformError:
    """把安全摘要附着到错误，供异步访问策略准确归因。"""

    if isinstance(proxy_id, str) and proxy_id:
        setattr(error, "proxy_id_summary", proxy_id)
    return error
