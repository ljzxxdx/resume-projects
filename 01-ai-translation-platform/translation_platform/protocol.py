"""secret 与 chat 请求的通用签名参数关系。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Optional, Tuple

from translation_platform.signer import JsSigner, SignatureResult, SignerError
from translation_platform.errors import SignatureTokenFailure


class ProtocolError(SignatureTokenFailure, ValueError):
    """端点配置或请求参数不符合协议关系时抛出。"""


class EndpointKind(str, Enum):
    """需要分别维护签名关系的端点类型。"""

    SECRET = "secret"
    CHAT = "chat"


@dataclass(frozen=True)
class EndpointSigningProfile:
    """一个端点不可变的签名字段配置。"""

    endpoint: EndpointKind
    keyid: str
    signing_key: str
    static_parameters: Mapping[str, str]
    signing_fields: Tuple[str, ...]
    point_param_fields: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.endpoint, EndpointKind):
            raise ProtocolError("endpoint must be a supported endpoint kind")
        _require_text("keyid", self.keyid)
        _require_text("signing_key", self.signing_key)
        static_parameters = _copy_text_mapping(
            self.static_parameters,
            "static_parameters",
        )
        signing_fields = _copy_field_sequence(
            self.signing_fields,
            "signing_fields",
        )
        point_param_fields = _copy_field_sequence(
            self.point_param_fields,
            "point_param_fields",
        )
        if not {"mysticTime", "key"}.issubset(signing_fields):
            raise ProtocolError("signing_fields must include mysticTime and key")
        if not set(point_param_fields).issubset(signing_fields):
            raise ProtocolError("point_param_fields must be part of signing_fields")
        if self.endpoint is EndpointKind.CHAT:
            required_chat_fields = {
                "keyid",
                "mysticTime",
                "token",
                "yduuid",
                "key",
            }
            if not required_chat_fields.issubset(signing_fields):
                raise ProtocolError(
                    "chat signing_fields must include keyid, mysticTime, token, "
                    "yduuid and key"
                )
            if not required_chat_fields.issubset(point_param_fields):
                raise ProtocolError(
                    "chat point_param_fields must include keyid, mysticTime, token, "
                    "yduuid and key"
                )

        object.__setattr__(self, "static_parameters", static_parameters)
        object.__setattr__(self, "signing_fields", signing_fields)
        object.__setattr__(self, "point_param_fields", point_param_fields)


@dataclass(frozen=True)
class SignedRequest:
    """已完成签名且可直接交给传输层的请求参数。"""

    endpoint: EndpointKind
    parameters: Mapping[str, str]
    signature: SignatureResult

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(self.parameters)),
        )


class SignedRequestBuilder:
    """按端点关系构造签名原文和实际请求参数。"""

    def __init__(self, signer: JsSigner) -> None:
        self._signer = signer

    def build_secret(
        self,
        profile: EndpointSigningProfile,
        yduuid: str,
        dynamic_parameters: Optional[Mapping[str, str]] = None,
    ) -> SignedRequest:
        """构造无需 token 的 secret 请求。"""

        if profile.endpoint is not EndpointKind.SECRET:
            raise ProtocolError("secret profile is required")
        return self._build(
            profile=profile,
            yduuid=_require_text("yduuid", yduuid),
            token=None,
            dynamic_parameters=dynamic_parameters,
        )

    def build_chat(
        self,
        profile: EndpointSigningProfile,
        token: str,
        yduuid: str,
        dynamic_parameters: Optional[Mapping[str, str]] = None,
    ) -> SignedRequest:
        """构造同时包含 token 与设备标识的 chat 请求。"""

        if profile.endpoint is not EndpointKind.CHAT:
            raise ProtocolError("chat profile is required")
        return self._build(
            profile=profile,
            yduuid=_require_text("yduuid", yduuid),
            token=_require_text("token", token),
            dynamic_parameters=dynamic_parameters,
        )

    def _build(
        self,
        profile: EndpointSigningProfile,
        yduuid: str,
        token: Optional[str],
        dynamic_parameters: Optional[Mapping[str, str]],
    ) -> SignedRequest:
        parameters = dict(profile.static_parameters)
        if dynamic_parameters is not None:
            parameters.update(
                _copy_text_mapping(dynamic_parameters, "dynamic_parameters")
            )

        # 调用方不能通过动态参数替换协议身份或注入签名结果。
        parameters.pop("key", None)
        parameters.pop("sign", None)
        parameters.pop("pointParam", None)
        parameters["keyid"] = profile.keyid
        parameters["yduuid"] = yduuid
        if token is None:
            parameters.pop("token", None)
        else:
            parameters["token"] = token

        try:
            signature = self._signer.sign(
                parameters=parameters,
                ordered_fields=profile.signing_fields,
                signing_key=profile.signing_key,
            )
        except SignerError as exc:
            raise ProtocolError(f"unable to sign request: {exc}") from exc

        # 签名器只读取一次时钟；这里复用其返回值，避免签名与请求时间戳分叉。
        parameters["mysticTime"] = signature.mystic_time
        parameters["sign"] = signature.signature
        parameters["pointParam"] = ",".join(profile.point_param_fields)
        parameters.pop("key", None)
        return SignedRequest(
            endpoint=profile.endpoint,
            parameters=parameters,
            signature=signature,
        )


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{name} must not be empty")
    return value


def _copy_text_mapping(
    value: Mapping[str, str],
    name: str,
) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{name} must be a mapping")
    copied = dict(value)
    if any(
        not isinstance(key, str)
        or not key
        or not isinstance(item, str)
        for key, item in copied.items()
    ):
        raise ProtocolError(f"{name} must contain text keys and values")
    return MappingProxyType(copied)


def _copy_field_sequence(value: object, name: str) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise ProtocolError(f"{name} must be a field sequence")
    try:
        fields = tuple(value)
    except TypeError as exc:
        raise ProtocolError(f"{name} must be a field sequence") from exc
    if not fields or any(not isinstance(field, str) or not field for field in fields):
        raise ProtocolError(f"{name} must contain non-empty field names")
    if len(fields) != len(set(fields)):
        raise ProtocolError(f"{name} must not contain duplicates")
    return fields
