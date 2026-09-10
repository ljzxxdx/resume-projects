from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from tests.test_client import FakeResponse
from tests.test_pacing import VirtualClock
from translation_platform.errors import ConfigurationFailure


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "validate_live_smoke.py"


def load_script_module():
    specification = importlib.util.spec_from_file_location(
        "validate_live_smoke",
        SCRIPT_PATH,
    )
    if specification is None or specification.loader is None:
        raise AssertionError("无法加载冒烟脚本")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


class SequenceSession:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.calls = []
        self.headers = {}
        self.proxies = {}
        self.trust_env = True
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return response

    def get(self, url, **kwargs):
        return self.post(url, **kwargs)

    def close(self) -> None:
        self.closed = True


class LiveSmokeScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.directory = Path(self.temporary_directory.name)
        self.config_path = self.directory / "live_smoke_config.json"
        self.config_path.write_text(
            json.dumps(self.valid_config(), ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def valid_config():
        return {
            "secret_url": "https://translation.invalid/secret",
            "business_url": "https://translation.invalid/chat",
            "business_parameter_location": "form",
            "yduuid": "fixture-device",
            "headers": {"X-Fixture": "offline"},
            "secret_request_method": "GET",
            "token_path": ["data", "credential"],
            "secret_profile": {
                "keyid": "fixture-secret-keyid",
                "signing_key": "fixture-secret-signing-key",
                "static_parameters": {"client": "fixture-client"},
                "signing_fields": ["client", "mysticTime", "key"],
                "point_param_fields": ["client", "mysticTime"],
            },
            "chat_profile": {
                "keyid": "fixture-chat-keyid",
                "signing_key": "fixture-chat-signing-key",
                "static_parameters": {"client": "fixture-client"},
                "signing_fields": [
                    "client",
                    "keyid",
                    "mysticTime",
                    "token",
                    "yduuid",
                    "key",
                ],
                "point_param_fields": [
                    "client",
                    "keyid",
                    "mysticTime",
                    "token",
                    "yduuid",
                    "key",
                ],
            },
            "sse": {"content_path": ["content"], "done_marker": "[DONE]"},
            "dynamic_parameters": {
                "base": {"mode": "fixture"},
                "text_field": "input",
                "source_language_field": "from",
                "target_language_field": "to",
            },
            "use_proxy": False,
            "proxy_fallback_enabled": False,
        }

    def test_config_check_builds_profiles_without_creating_session(self) -> None:
        module = load_script_module()

        config = module.load_live_smoke_config(self.config_path)

        self.assertEqual(config.secret_request_method, "GET")
        self.assertEqual(config.token_path, ("data", "credential"))
        self.assertEqual(config.secret_profile.endpoint.value, "secret")
        self.assertEqual(config.chat_profile.endpoint.value, "chat")
        self.assertFalse(config.use_proxy)
        self.assertEqual(module.main(["--config", str(self.config_path), "--check-config"]), 0)

    def test_config_rejects_proxy_or_incomplete_dynamic_mapping_offline(self) -> None:
        module = load_script_module()
        invalid_cases = []
        proxy_config = self.valid_config()
        proxy_config["use_proxy"] = True
        invalid_cases.append(proxy_config)
        missing_field = self.valid_config()
        del missing_field["dynamic_parameters"]["text_field"]
        invalid_cases.append(missing_field)

        for index, payload in enumerate(invalid_cases):
            with self.subTest(index=index):
                path = self.directory / ("invalid-" + str(index) + ".json")
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ConfigurationFailure):
                    module.load_live_smoke_config(path)

    def test_fake_session_uses_full_production_chain_and_lifecycle(self) -> None:
        module = load_script_module()
        success_response = lambda: FakeResponse(
            stream_lines=[
                b'data: {"content":"fixture translation"}',
                b"",
                b"data: [DONE]",
            ]
        )
        session = SequenceSession(
            [FakeResponse(payload={"data": {"credential": "fixture-auth-value"}})]
            + [success_response() for _ in range(20)]
        )
        checkpoint_path = self.directory / "private" / "checkpoint.jsonl"
        evidence_path = self.directory / "public" / "evidence.json"
        clock = VirtualClock()

        evidence = module.run_live_smoke(
            module.load_live_smoke_config(self.config_path),
            checkpoint_path=checkpoint_path,
            evidence_path=evidence_path,
            session=session,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )

        self.assertTrue(session.closed)
        self.assertEqual(len(session.calls), 21)
        self.assertTrue(session.calls[0][0].endswith("/secret"))
        self.assertEqual(evidence["status"], "available")
        self.assertEqual(json.loads(evidence_path.read_text(encoding="utf-8")), evidence)
        self.assertTrue(checkpoint_path.is_file())
        self.assertEqual(
            [timestamp for timestamp in clock.sleeps if timestamp >= 1.0],
            [1.0] * 20,
        )
        chat_calls = [call for call in session.calls if call[0].endswith("/chat")]
        self.assertEqual(len(chat_calls), 20)
        self.assertEqual(chat_calls[0][1]["data"]["from"], "zh-CHS")
        self.assertEqual(chat_calls[1][1]["data"]["from"], "en")
        self.assertTrue(all("params" not in call[1] for call in chat_calls))
        self.assertTrue(all(call[1]["stream"] for call in chat_calls))
        self.assertTrue(all(call[1]["timeout"] == (3.05, 15.0) for call in session.calls))
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies, {})


if __name__ == "__main__":
    unittest.main()
