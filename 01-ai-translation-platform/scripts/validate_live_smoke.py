"""以私有配置运行通用低负载双向冒烟。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Dict, NamedTuple, Optional, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from translation_platform.access import AccessFallbackController, AccessPolicy
from translation_platform.checkpoint import JsonlCheckpointStore
from translation_platform.client import (
    AuthenticatedTranslationClient,
    BusinessRequestClient,
    JsonTokenProvider,
    RequestRuntime,
)
from translation_platform.config import RuntimeConfig
from translation_platform.errors import (
    ConfigurationFailure,
    EmptyTranslationError,
    ErrorType,
)
from translation_platform.live_smoke import LiveSmokeRunner, SmokeAttempt, SmokeSample
from translation_platform.paths import resolve_project_path
from translation_platform.protocol import (
    EndpointKind,
    EndpointSigningProfile,
    SignedRequestBuilder,
)
from translation_platform.signer import JsSigner
from translation_platform.sse import SseParser
from translation_platform.token import TokenManager


DEFAULT_CONFIG_PATH = resolve_project_path(
    "artifacts", "private", "validation", "live_smoke_config.json"
)
DEFAULT_CHECKPOINT_PATH = resolve_project_path(
    "artifacts", "private", "validation", "live_smoke_checkpoint.jsonl"
)
DEFAULT_EVIDENCE_PATH = resolve_project_path(
    "artifacts", "validation", "live_smoke_evidence.json"
)


class DynamicParameterConfig(NamedTuple):
    base: Mapping[str, str]
    text_field: str
    source_language_field: str
    target_language_field: str


class LiveSmokeConfig(NamedTuple):
    secret_url: str
    business_url: str
    business_parameter_location: str
    yduuid: str
    headers: Mapping[str, str]
    secret_request_method: str
    token_path: Sequence[str]
    secret_profile: EndpointSigningProfile
    chat_profile: EndpointSigningProfile
    sse_content_path: Sequence[str]
    sse_done_marker: str
    dynamic_parameters: DynamicParameterConfig
    use_proxy: bool
    proxy_fallback_enabled: bool


def load_live_smoke_config(path: Path) -> LiveSmokeConfig:
    """只读取并校验私有配置，不创建 Session 或发送请求。"""

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        raise ConfigurationFailure("无法读取私有冒烟配置") from None
    except (UnicodeError, json.JSONDecodeError):
        raise ConfigurationFailure("私有冒烟配置不是有效 UTF-8 JSON") from None

    root = _require_mapping(payload, "私有冒烟配置")
    _require_exact_fields(
        root,
        {
            "secret_url",
            "business_url",
            "business_parameter_location",
            "yduuid",
            "headers",
            "secret_request_method",
            "token_path",
            "secret_profile",
            "chat_profile",
            "sse",
            "dynamic_parameters",
            "use_proxy",
            "proxy_fallback_enabled",
        },
        "私有冒烟配置",
    )
    use_proxy = _require_boolean(root["use_proxy"], "use_proxy")
    fallback_enabled = _require_boolean(
        root["proxy_fallback_enabled"],
        "proxy_fallback_enabled",
    )
    if use_proxy or fallback_enabled:
        raise ConfigurationFailure("真实冒烟禁止启用代理或访问限制备用")

    sse = _require_mapping(root["sse"], "sse")
    _require_exact_fields(sse, {"content_path", "done_marker"}, "sse")
    content_path = _require_text_sequence(sse["content_path"], "sse.content_path")

    dynamic = _require_mapping(root["dynamic_parameters"], "dynamic_parameters")
    _require_exact_fields(
        dynamic,
        {"base", "text_field", "source_language_field", "target_language_field"},
        "dynamic_parameters",
    )

    try:
        secret_profile = _profile(root["secret_profile"], EndpointKind.SECRET)
        chat_profile = _profile(root["chat_profile"], EndpointKind.CHAT)
    except ConfigurationFailure:
        raise
    except Exception:
        raise ConfigurationFailure("签名 profile 结构无效") from None

    return LiveSmokeConfig(
        secret_url=_require_text(root["secret_url"], "secret_url"),
        business_url=_require_text(root["business_url"], "business_url"),
        business_parameter_location=_require_parameter_location(
            root["business_parameter_location"]
        ),
        yduuid=_require_text(root["yduuid"], "yduuid"),
        headers=_require_text_mapping(root["headers"], "headers"),
        secret_request_method=_require_http_method(root["secret_request_method"]),
        token_path=_require_text_sequence(root["token_path"], "token_path"),
        secret_profile=secret_profile,
        chat_profile=chat_profile,
        sse_content_path=content_path,
        sse_done_marker=_require_text(sse["done_marker"], "sse.done_marker"),
        dynamic_parameters=DynamicParameterConfig(
            base=_require_text_mapping(dynamic["base"], "dynamic_parameters.base"),
            text_field=_require_text(dynamic["text_field"], "dynamic_parameters.text_field"),
            source_language_field=_require_text(
                dynamic["source_language_field"],
                "dynamic_parameters.source_language_field",
            ),
            target_language_field=_require_text(
                dynamic["target_language_field"],
                "dynamic_parameters.target_language_field",
            ),
        ),
        use_proxy=use_proxy,
        proxy_fallback_enabled=fallback_enabled,
    )


def run_live_smoke(
    config: LiveSmokeConfig,
    *,
    checkpoint_path: Path = DEFAULT_CHECKPOINT_PATH,
    evidence_path: Path = DEFAULT_EVIDENCE_PATH,
    session: Optional[Any] = None,
    monotonic: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> Mapping[str, object]:
    """装配既有生产链并在统一生命周期内执行冒烟。"""

    if not isinstance(config, LiveSmokeConfig):
        raise ConfigurationFailure("config 必须是 LiveSmokeConfig")
    checkpoint_path = Path(checkpoint_path)
    evidence_path = Path(evidence_path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.parent.mkdir(parents=True, exist_ok=True)

    runtime = RequestRuntime(
        RuntimeConfig(
            from_lang="zh-CHS",
            to_lang="en",
            request_timeout_seconds=15.0,
            min_interval_seconds=1.0,
            max_retries=1,
            workers=1,
            use_proxy=False,
        ),
        monotonic=monotonic,
        sleep=sleep,
    )
    transport = runtime.create_http_client(session=session, headers=config.headers)
    request_builder = SignedRequestBuilder(JsSigner())
    token_manager = TokenManager(
        JsonTokenProvider(
            transport=transport,
            request_builder=request_builder,
            profile=config.secret_profile,
            secret_url=config.secret_url,
            yduuid=config.yduuid,
            request_method=config.secret_request_method,
            token_path=config.token_path,
        )
    )
    authenticated_client = AuthenticatedTranslationClient(
        token_manager=token_manager,
        request_builder=request_builder,
        chat_profile=config.chat_profile,
        business_client=BusinessRequestClient(
            transport,
            config.business_url,
            parameter_location=config.business_parameter_location,
        ),
        yduuid=config.yduuid,
        sse_parser=SseParser(
            content_path=config.sse_content_path,
            done_marker=config.sse_done_marker,
        ),
    )
    access_controller = AccessFallbackController(
        AccessPolicy(proxy_fallback_enabled=False, max_proxy_switches=0),
        switch_proxy=transport.proxy_manager.handle_failure,
    )
    stable_client = runtime.create_stable_translation_client(
        authenticated_client,
        access_controller,
    )
    timer = time.monotonic if monotonic is None else monotonic

    def translate(sample: SmokeSample) -> SmokeAttempt:
        parameters: Dict[str, str] = dict(config.dynamic_parameters.base)
        parameters[config.dynamic_parameters.text_field] = sample.text
        parameters[config.dynamic_parameters.source_language_field] = sample.source_lang
        parameters[config.dynamic_parameters.target_language_field] = sample.target_lang
        started = timer()
        outcome = stable_client.post_translation(parameters)
        latency_ms = max(0.0, (timer() - started) * 1000)
        retries = len(outcome.retry_delays)
        if outcome.succeeded and outcome.value is not None:
            content = outcome.value.content.strip()
            if content:
                return SmokeAttempt(
                    translated_text=content,
                    error_type=None,
                    request_attempts=outcome.request_attempts,
                    retries=retries,
                    proxy_switches=outcome.proxy_switches,
                    latency_ms=latency_ms,
                )
            error_type = ErrorType.EMPTY_RESULT
        elif outcome.failure is not None:
            error_type = outcome.failure.error_type
        else:
            error_type = ErrorType.RESPONSE_FORMAT
        return SmokeAttempt(
            translated_text=None,
            error_type=error_type,
            request_attempts=max(1, outcome.request_attempts),
            retries=retries,
            proxy_switches=outcome.proxy_switches,
            latency_ms=latency_ms,
        )

    checkpoint_store = JsonlCheckpointStore(checkpoint_path)
    runner = LiveSmokeRunner(
        translate=translate,
        checkpoint_store=checkpoint_store,
        proxy_enabled=False,
    )
    result_holder: Dict[str, Mapping[str, object]] = {}

    def operation() -> Mapping[str, object]:
        result = runner.run()
        result_holder["evidence"] = result
        return result

    def save_checkpoint(_state: object) -> None:
        records = checkpoint_store.load()
        if records:
            checkpoint_store.save(records)

    def write_summary(_state: object) -> None:
        evidence = result_holder.get("evidence")
        if evidence is not None:
            _write_json_atomic(evidence_path, evidence)

    return runtime.execute_managed(
        client=transport,
        operation=operation,
        save_checkpoint=save_checkpoint,
        write_summary=write_summary,
    )


def _profile(value: object, endpoint: EndpointKind) -> EndpointSigningProfile:
    profile = _require_mapping(value, endpoint.value + "_profile")
    _require_exact_fields(
        profile,
        {
            "keyid",
            "signing_key",
            "static_parameters",
            "signing_fields",
            "point_param_fields",
        },
        endpoint.value + "_profile",
    )
    return EndpointSigningProfile(
        endpoint=endpoint,
        keyid=_require_text(profile["keyid"], endpoint.value + "_profile.keyid"),
        signing_key=_require_text(
            profile["signing_key"], endpoint.value + "_profile.signing_key"
        ),
        static_parameters=_require_text_mapping(
            profile["static_parameters"], endpoint.value + "_profile.static_parameters"
        ),
        signing_fields=_require_text_sequence(
            profile["signing_fields"], endpoint.value + "_profile.signing_fields"
        ),
        point_param_fields=_require_text_sequence(
            profile["point_param_fields"], endpoint.value + "_profile.point_param_fields"
        ),
    )


def _require_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigurationFailure(name + " 必须是对象")
    if any(not isinstance(key, str) for key in value):
        raise ConfigurationFailure(name + " 的字段名必须是文本")
    return dict(value)


def _require_exact_fields(value: Mapping[str, object], expected: set, name: str) -> None:
    if set(value) != expected:
        raise ConfigurationFailure(name + " 的字段集合不完整或包含未知字段")


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationFailure(name + " 必须是非空文本")
    return value.strip()


def _require_text_mapping(value: object, name: str) -> Mapping[str, str]:
    mapping = _require_mapping(value, name)
    if any(
        not key.strip() or not isinstance(item, str) or not item.strip()
        for key, item in mapping.items()
    ):
        raise ConfigurationFailure(name + " 必须只包含非空文本键值")
    return {key: item for key, item in mapping.items()}


def _require_text_sequence(value: object, name: str) -> Sequence[str]:
    if isinstance(value, (str, bytes)):
        raise ConfigurationFailure(name + " 必须是文本数组")
    try:
        items = tuple(value)
    except TypeError:
        raise ConfigurationFailure(name + " 必须是文本数组") from None
    if not items or any(not isinstance(item, str) or not item for item in items):
        raise ConfigurationFailure(name + " 必须是非空文本数组")
    return items


def _require_boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigurationFailure(name + " 必须是布尔值")
    return value


def _require_http_method(value: object) -> str:
    method = _require_text(value, "secret_request_method").upper()
    if method not in {"GET", "POST"}:
        raise ConfigurationFailure("secret_request_method 只支持 GET 或 POST")
    return method


def _require_parameter_location(value: object) -> str:
    location = _require_text(value, "business_parameter_location").lower()
    if location not in {"query", "form"}:
        raise ConfigurationFailure(
            "business_parameter_location 只支持 query 或 form"
        )
    return location


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(
                payload,
                temporary_file,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(str(temporary_path), str(path))
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行低负载双向真实冒烟")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT_PATH)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="仅离线校验私有配置结构，不发送请求",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        config = load_live_smoke_config(arguments.config)
        if arguments.check_config:
            print("配置结构校验通过（未发送网络请求）")
            return 0
        evidence = run_live_smoke(
            config,
            checkpoint_path=arguments.checkpoint,
            evidence_path=arguments.evidence,
        )
    except ConfigurationFailure as exc:
        print("冒烟校验失败：" + str(exc), file=sys.stderr)
        return 2
    return 0 if evidence["status"] == "available" else 3


if __name__ == "__main__":
    raise SystemExit(main())
