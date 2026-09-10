from __future__ import annotations

import importlib.util
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SignerAvailabilityTests(unittest.TestCase):
    def test_signer_module_and_javascript_source_are_available(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("translation_platform.signer"))
        self.assertTrue((PROJECT_ROOT / "js" / "sign.js").is_file())


class SignerBehaviorTests(unittest.TestCase):
    def signing_arguments(self):
        return {
            "parameters": {
                "client": "synthetic-client",
                "product": "synthetic-product",
            },
            "ordered_fields": ("client", "mysticTime", "product", "key"),
            "signing_key": "synthetic-key",
        }

    def test_fixed_clock_produces_repeatable_payload_and_signature(self) -> None:
        from translation_platform.signer import JsSigner

        signer = JsSigner(clock=lambda: 1700000000123)

        first = signer.sign(**self.signing_arguments())
        second = signer.sign(**self.signing_arguments())

        self.assertEqual(first, second)
        self.assertEqual(first.mystic_time, "1700000000123")
        self.assertEqual(
            first.payload,
            "client=synthetic-client&mysticTime=1700000000123&"
            "product=synthetic-product&key=synthetic-key",
        )
        self.assertEqual(first.signature, "77248b6033ef33333853997c754eee6a")

    def test_real_execjs_round_trip_preserves_chinese_payload_and_signature(self) -> None:
        from translation_platform.signer import JsSigner

        result = JsSigner(clock=lambda: 1700000000123).sign(
            parameters={
                "client": "synthetic-client",
                "input": "天气晴朗。",
            },
            ordered_fields=("client", "input", "mysticTime", "key"),
            signing_key="synthetic-key",
        )

        self.assertEqual(
            result.payload,
            "client=synthetic-client&input=天气晴朗。&"
            "mysticTime=1700000000123&key=synthetic-key",
        )
        self.assertEqual(result.signature, "6a508329a1bbe3f435f6c8b95e0e25f4")

    def test_injected_clock_replaces_stale_timestamp_and_is_called_once(self) -> None:
        from translation_platform.signer import JsSigner

        calls = []

        def fixed_clock() -> int:
            calls.append("called")
            return 1700000000456

        arguments = self.signing_arguments()
        arguments["parameters"]["mysticTime"] = "stale-value"

        result = JsSigner(clock=fixed_clock).sign(**arguments)

        self.assertEqual(calls, ["called"])
        self.assertEqual(result.mystic_time, "1700000000456")
        self.assertIn("mysticTime=1700000000456", result.payload)
        self.assertNotIn("stale-value", result.payload)

    def test_javascript_is_compiled_once_during_concurrent_construction(self) -> None:
        import execjs

        from translation_platform.signer import JsSigner

        source = (PROJECT_ROOT / "js" / "sign.js").read_text(encoding="utf-8")
        compile_calls = []
        worker_count = 8
        start_barrier = threading.Barrier(worker_count)

        def recording_compiler(javascript_source: str):
            compile_calls.append(javascript_source)
            time.sleep(0.05)
            return execjs.compile(javascript_source)

        def build_signer(script_path: Path):
            start_barrier.wait()
            return JsSigner(
                js_path=script_path,
                clock=lambda: 1700000000123,
                compiler=recording_compiler,
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            script_path = Path(temporary_directory) / "sign.js"
            script_path.write_text(source, encoding="utf-8")

            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                signers = list(
                    executor.map(
                        build_signer,
                        [script_path] * worker_count,
                    )
                )
            for signer in signers:
                signer.sign(**self.signing_arguments())

        self.assertEqual(len(compile_calls), 1)

    def test_missing_parameter_and_invalid_clock_are_rejected(self) -> None:
        from translation_platform.signer import JsSigner, SignerError

        arguments = self.signing_arguments()
        del arguments["parameters"]["product"]
        with self.assertRaisesRegex(SignerError, "missing signing parameter: product"):
            JsSigner(clock=lambda: 1700000000123).sign(**arguments)

        with self.assertRaisesRegex(SignerError, "clock must return"):
            JsSigner(clock=lambda: -1).sign(**self.signing_arguments())

    def test_ordered_fields_must_include_timestamp_and_key(self) -> None:
        from translation_platform.signer import JsSigner, SignerError

        arguments = self.signing_arguments()
        arguments["ordered_fields"] = ("client", "product")

        with self.assertRaisesRegex(
            SignerError,
            "ordered signing fields must include mysticTime and key",
        ):
            JsSigner(clock=lambda: 1700000000123).sign(**arguments)

    def test_invalid_parameters_and_runtime_failures_are_wrapped(self) -> None:
        import execjs

        from translation_platform.signer import JsSigner, SignerError

        with self.assertRaisesRegex(SignerError, "parameters must be a mapping"):
            JsSigner(clock=lambda: 1700000000123).sign(
                parameters=None,
                ordered_fields=("mysticTime", "key"),
                signing_key="synthetic-key",
            )

        arguments = self.signing_arguments()
        arguments["parameters"]["client"] = object()
        with self.assertRaisesRegex(SignerError, "JSON-compatible"):
            JsSigner(clock=lambda: 1700000000123).sign(**arguments)

        class FailingContext:
            def call(self, *_args):
                raise execjs.ProgramError("synthetic JavaScript failure")

        def failing_compiler(_source: str):
            return FailingContext()

        with self.assertRaisesRegex(SignerError, "JavaScript signing failed"):
            JsSigner(
                clock=lambda: 1700000000123,
                compiler=failing_compiler,
            ).sign(**self.signing_arguments())

    def test_signing_key_replaces_stale_parameter_value(self) -> None:
        from translation_platform.signer import JsSigner

        arguments = self.signing_arguments()
        arguments["parameters"]["key"] = "stale-key"

        result = JsSigner(clock=lambda: 1700000000123).sign(**arguments)

        self.assertIn("key=synthetic-key", result.payload)
        self.assertNotIn("stale-key", result.payload)

    def test_falsey_callable_clock_is_not_replaced(self) -> None:
        from translation_platform.signer import JsSigner

        class FalseyClock:
            def __bool__(self) -> bool:
                return False

            def __call__(self) -> int:
                return 1700000000789

        result = JsSigner(clock=FalseyClock()).sign(**self.signing_arguments())

        self.assertEqual(result.mystic_time, "1700000000789")


if __name__ == "__main__":
    unittest.main()
