from __future__ import annotations

import importlib.util
import unittest


class ProtocolAvailabilityTests(unittest.TestCase):
    def test_protocol_module_is_available(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("translation_platform.protocol"))


class SignedRequestBuilderTests(unittest.TestCase):
    def secret_profile(self):
        from translation_platform.protocol import EndpointKind, EndpointSigningProfile

        return EndpointSigningProfile(
            endpoint=EndpointKind.SECRET,
            keyid="synthetic-secret-keyid",
            signing_key="synthetic-secret-key",
            static_parameters={
                "client": "synthetic-client",
                "product": "synthetic-product",
            },
            signing_fields=("client", "mysticTime", "product", "key"),
            point_param_fields=("client", "mysticTime", "product"),
        )

    def chat_profile(self, static_parameters=None):
        from translation_platform.protocol import EndpointKind, EndpointSigningProfile

        return EndpointSigningProfile(
            endpoint=EndpointKind.CHAT,
            keyid="synthetic-chat-keyid",
            signing_key="synthetic-chat-key",
            static_parameters=(
                {"client": "synthetic-client"}
                if static_parameters is None
                else static_parameters
            ),
            signing_fields=(
                "client",
                "keyid",
                "mysticTime",
                "token",
                "yduuid",
                "key",
            ),
            point_param_fields=(
                "client",
                "keyid",
                "mysticTime",
                "token",
                "yduuid",
                "key",
            ),
        )

    def builder(self):
        from translation_platform.protocol import SignedRequestBuilder
        from translation_platform.signer import JsSigner

        return SignedRequestBuilder(JsSigner(clock=lambda: 1700000000123))

    def test_secret_request_reuses_signed_timestamp_without_exposing_key(self) -> None:
        request = self.builder().build_secret(
            profile=self.secret_profile(),
            yduuid="synthetic-device-id",
            dynamic_parameters={"mysticTime": "stale-value"},
        )

        self.assertEqual(request.parameters["mysticTime"], "1700000000123")
        self.assertIn("mysticTime=1700000000123", request.signature.payload)
        self.assertNotIn("stale-value", request.signature.payload)
        self.assertEqual(request.parameters["sign"], request.signature.signature)
        self.assertEqual(request.parameters["keyid"], "synthetic-secret-keyid")
        self.assertEqual(
            request.parameters["pointParam"],
            "client,mysticTime,product",
        )
        self.assertEqual(request.parameters["yduuid"], "synthetic-device-id")
        self.assertNotIn("token", request.parameters)
        self.assertNotIn("key", request.parameters)

    def test_chat_request_signs_token_and_device_identifier(self) -> None:
        request = self.builder().build_chat(
            profile=self.chat_profile(),
            token="synthetic-token",
            yduuid="synthetic-device-id",
            dynamic_parameters={"input": "synthetic text"},
        )

        self.assertEqual(request.parameters["mysticTime"], "1700000000123")
        self.assertIn("token=synthetic-token", request.signature.payload)
        self.assertIn("yduuid=synthetic-device-id", request.signature.payload)
        self.assertIn("key=synthetic-chat-key", request.signature.payload)
        self.assertNotIn("key", request.parameters)
        self.assertEqual(
            request.parameters["pointParam"],
            "client,keyid,mysticTime,token,yduuid,key",
        )

    def test_endpoint_relationships_and_required_values_are_enforced(self) -> None:
        from translation_platform.protocol import ProtocolError

        with self.assertRaisesRegex(ProtocolError, "secret profile"):
            self.builder().build_secret(
                profile=self.chat_profile(),
                yduuid="synthetic-device-id",
            )
        with self.assertRaisesRegex(ProtocolError, "chat profile"):
            self.builder().build_chat(
                profile=self.secret_profile(),
                token="synthetic-token",
                yduuid="synthetic-device-id",
            )
        with self.assertRaisesRegex(ProtocolError, "token must not be empty"):
            self.builder().build_chat(
                profile=self.chat_profile(),
                token=" ",
                yduuid="synthetic-device-id",
            )
        with self.assertRaisesRegex(ProtocolError, "yduuid must not be empty"):
            self.builder().build_secret(
                profile=self.secret_profile(),
                yduuid="",
            )

    def test_profile_and_built_parameters_are_immutable_copies(self) -> None:
        profile_source = {"client": "synthetic-client"}
        profile = self.chat_profile(profile_source)
        dynamic_source = {"input": "synthetic text"}
        request = self.builder().build_chat(
            profile=profile,
            token="synthetic-token",
            yduuid="synthetic-device-id",
            dynamic_parameters=dynamic_source,
        )

        profile_source["client"] = "changed"
        dynamic_source["input"] = "changed"
        self.assertEqual(profile.static_parameters["client"], "synthetic-client")
        self.assertEqual(request.parameters["input"], "synthetic text")
        with self.assertRaises(TypeError):
            request.parameters["input"] = "changed"

    def test_chat_profile_must_sign_all_authentication_identifiers(self) -> None:
        from translation_platform.protocol import (
            EndpointKind,
            EndpointSigningProfile,
            ProtocolError,
        )

        for missing_field in ("keyid", "token", "yduuid"):
            signing_fields = tuple(
                field
                for field in ("keyid", "mysticTime", "token", "yduuid", "key")
                if field != missing_field
            )
            with self.subTest(missing_field=missing_field):
                with self.assertRaisesRegex(ProtocolError, "chat signing_fields"):
                    EndpointSigningProfile(
                        endpoint=EndpointKind.CHAT,
                        keyid="synthetic-chat-keyid",
                        signing_key="synthetic-chat-key",
                        static_parameters={},
                        signing_fields=signing_fields,
                        point_param_fields=signing_fields,
                    )

        with self.assertRaisesRegex(ProtocolError, "chat point_param_fields"):
            EndpointSigningProfile(
                endpoint=EndpointKind.CHAT,
                keyid="synthetic-chat-keyid",
                signing_key="synthetic-chat-key",
                static_parameters={},
                signing_fields=("keyid", "mysticTime", "token", "yduuid", "key"),
                point_param_fields=("keyid", "mysticTime", "yduuid", "key"),
            )


if __name__ == "__main__":
    unittest.main()
