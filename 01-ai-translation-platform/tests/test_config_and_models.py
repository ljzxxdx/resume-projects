from __future__ import annotations

import importlib.util
import math
import unittest
from dataclasses import FrozenInstanceError


class ModuleAvailabilityTests(unittest.TestCase):
    def test_config_and_models_modules_are_available(self) -> None:
        self.assertIsNotNone(importlib.util.find_spec("translation_platform.config"))
        self.assertIsNotNone(importlib.util.find_spec("translation_platform.models"))


class RuntimeConfigTests(unittest.TestCase):
    def test_valid_config_is_immutable_and_keeps_low_load_defaults(self) -> None:
        from translation_platform.config import RuntimeConfig

        config = RuntimeConfig(from_lang="en", to_lang="zh-CHS")

        self.assertEqual(config.request_timeout_seconds, 30.0)
        self.assertEqual(config.min_interval_seconds, 1.0)
        self.assertEqual(config.max_retries, 3)
        self.assertEqual(config.workers, 1)
        self.assertFalse(config.use_proxy)
        self.assertIsNone(config.proxy_reference)
        with self.assertRaises(FrozenInstanceError):
            config.workers = 2

    def test_languages_are_limited_and_must_differ(self) -> None:
        from translation_platform.config import ConfigurationError, RuntimeConfig

        with self.assertRaisesRegex(ConfigurationError, "unsupported language"):
            RuntimeConfig(from_lang="fr", to_lang="en")
        with self.assertRaisesRegex(ConfigurationError, "languages must differ"):
            RuntimeConfig(from_lang="en", to_lang="en")

    def test_numeric_config_boundaries_are_enforced(self) -> None:
        from translation_platform.config import ConfigurationError, RuntimeConfig

        invalid_options = (
            {"request_timeout_seconds": 0},
            {"request_timeout_seconds": math.inf},
            {"min_interval_seconds": 0.99},
            {"min_interval_seconds": 61},
            {"max_retries": -1},
            {"max_retries": 6},
            {"workers": 0},
            {"workers": 3},
        )
        for options in invalid_options:
            with self.subTest(options=options):
                with self.assertRaises(ConfigurationError):
                    RuntimeConfig(from_lang="en", to_lang="zh-CHS", **options)

    def test_proxy_use_requires_a_private_reference(self) -> None:
        from translation_platform.config import ConfigurationError, RuntimeConfig

        with self.assertRaisesRegex(ConfigurationError, "proxy reference is required"):
            RuntimeConfig(from_lang="en", to_lang="zh-CHS", use_proxy=True)

        config = RuntimeConfig(
            from_lang="en",
            to_lang="zh-CHS",
            use_proxy=True,
            proxy_reference="private-proxy-reference",
        )
        self.assertEqual(config.proxy_reference, "private-proxy-reference")

        with self.assertRaises(ConfigurationError):
            RuntimeConfig(
                from_lang="en",
                to_lang="zh-CHS",
                use_proxy=True,
                proxy_reference=123,
            )


class DataModelTests(unittest.TestCase):
    def test_translation_result_records_required_trace_fields(self) -> None:
        from translation_platform.models import RecordStatus, TranslationResult

        result = TranslationResult(
            run_id="run-synthetic-001",
            input_summary="sha256:synthetic-input",
            from_lang="en",
            to_lang="zh-CHS",
            status=RecordStatus.SUCCESS,
            attempts=1,
            proxy_id_summary=None,
            latency_ms=125.5,
            error_type=None,
            translated_text="合成结果",
        )

        self.assertEqual(result.status, RecordStatus.SUCCESS)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.latency_ms, 125.5)
        with self.assertRaises(FrozenInstanceError):
            result.attempts = 2

    def test_translation_result_requires_successful_nonempty_output(self) -> None:
        from translation_platform.models import (
            ErrorType,
            ModelValidationError,
            RecordStatus,
            TranslationResult,
        )

        base = {
            "run_id": "run-synthetic-001",
            "input_summary": "sha256:synthetic-input",
            "from_lang": "en",
            "to_lang": "zh-CHS",
            "status": RecordStatus.SUCCESS,
            "attempts": 1,
            "proxy_id_summary": None,
            "latency_ms": 10.0,
            "error_type": None,
            "translated_text": "合成结果",
        }
        invalid_overrides = (
            {"status": RecordStatus.FAILURE},
            {"translated_text": "   "},
            {"translated_text": None},
            {"error_type": ErrorType.NETWORK_CONNECTION},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ModelValidationError):
                    TranslationResult(**{**base, **overrides})

    def test_failure_record_requires_error_details(self) -> None:
        from translation_platform.models import (
            ErrorType,
            FailureRecord,
            ModelValidationError,
            RecordStatus,
        )

        failure = FailureRecord(
            run_id="run-synthetic-002",
            input_summary="sha256:synthetic-input",
            from_lang="zh-CHS",
            to_lang="en",
            status=RecordStatus.FAILURE,
            attempts=2,
            proxy_id_summary="proxy-a1b2",
            latency_ms=250.0,
            error_type=ErrorType.TIMEOUT,
            error_message="synthetic timeout",
        )
        self.assertEqual(failure.error_type, ErrorType.TIMEOUT)
        self.assertEqual(failure.proxy_id_summary, "proxy-a1b2")

        with self.assertRaises(ModelValidationError):
            FailureRecord(
                run_id="run-synthetic-002",
                input_summary="sha256:synthetic-input",
                from_lang="zh-CHS",
                to_lang="en",
                status=RecordStatus.FAILURE,
                attempts=2,
                proxy_id_summary=None,
                latency_ms=250.0,
                error_type=None,
                error_message="",
            )
        with self.assertRaises(ModelValidationError):
            FailureRecord(
                run_id="run-synthetic-002",
                input_summary="sha256:synthetic-input",
                from_lang="zh-CHS",
                to_lang="en",
                status=RecordStatus.FAILURE,
                attempts=2,
                proxy_id_summary=None,
                latency_ms=250.0,
                error_type=ErrorType.TIMEOUT,
                error_message=None,
            )

    def test_run_summary_accepts_partial_status_and_zero_attempts(self) -> None:
        from translation_platform.models import ErrorType, RecordStatus, RunSummary

        summary = RunSummary(
            run_id="run-synthetic-003",
            input_summary="cells=2,unique=2",
            from_lang="zh-CHS",
            to_lang="en",
            status=RecordStatus.PARTIAL,
            attempts=0,
            proxy_id_summary=None,
            latency_ms=0.0,
            error_type=ErrorType.TIMEOUT,
        )

        self.assertEqual(summary.status, RecordStatus.PARTIAL)
        self.assertEqual(summary.attempts, 0)
        with self.assertRaises(FrozenInstanceError):
            summary.status = RecordStatus.SUCCESS

    def test_common_trace_fields_reject_invalid_values(self) -> None:
        from translation_platform.models import (
            ModelValidationError,
            RecordStatus,
            RunSummary,
        )

        base = {
            "run_id": "run-synthetic-004",
            "input_summary": "cells=1,unique=1",
            "from_lang": "en",
            "to_lang": "zh-CHS",
            "status": RecordStatus.SUCCESS,
            "attempts": 1,
            "proxy_id_summary": None,
            "latency_ms": 15.0,
            "error_type": None,
        }
        invalid_overrides = (
            {"run_id": "   "},
            {"input_summary": ""},
            {"from_lang": "fr"},
            {"to_lang": "en"},
            {"attempts": -1},
            {"latency_ms": -0.1},
            {"latency_ms": math.inf},
            {"status": "success"},
            {"error_type": "timeout"},
            {"proxy_id_summary": ""},
            {"proxy_id_summary": 123},
        )
        for overrides in invalid_overrides:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ModelValidationError):
                    RunSummary(**{**base, **overrides})


if __name__ == "__main__":
    unittest.main()
