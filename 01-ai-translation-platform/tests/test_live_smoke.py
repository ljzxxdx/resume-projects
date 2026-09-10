from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from translation_platform.cache import build_cache_key
from translation_platform.checkpoint import (
    CheckpointRecord,
    CheckpointStatus,
    JsonlCheckpointStore,
)
from translation_platform.errors import ErrorType


class SequenceTranslator:
    """只替代外部请求，保留真实冒烟编排和检查点写入。"""

    def __init__(self, outcomes) -> None:
        self.outcomes = iter(outcomes)
        self.calls = []

    def __call__(self, sample):
        self.calls.append(sample.sample_id)
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class LiveSmokeRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.checkpoint_store = JsonlCheckpointStore(
            Path(self.temporary_directory.name) / "live-smoke.jsonl"
        )
        self.started_at = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)
        self.finished_at = self.started_at + timedelta(seconds=20)

    @staticmethod
    def success(text="synthetic translated text", attempts=1, retries=0):
        from translation_platform.live_smoke import SmokeAttempt

        return SmokeAttempt(
            translated_text=text,
            error_type=None,
            request_attempts=attempts,
            retries=retries,
            proxy_switches=0,
            latency_ms=10.0,
        )

    @staticmethod
    def failure(error_type, attempts=1, retries=0):
        from translation_platform.live_smoke import SmokeAttempt

        return SmokeAttempt(
            translated_text=None,
            error_type=error_type,
            request_attempts=attempts,
            retries=retries,
            proxy_switches=0,
            latency_ms=8.0,
        )

    def run_smoke(self, outcomes):
        from translation_platform.live_smoke import LiveSmokeRunner

        translator = SequenceTranslator(outcomes)
        clock_values = iter((self.started_at, self.finished_at))
        evidence = LiveSmokeRunner(
            translate=translator,
            checkpoint_store=self.checkpoint_store,
            now=lambda: next(clock_values),
            proxy_enabled=False,
        ).run()
        return evidence, translator

    def test_available_executes_fixed_twenty_samples_and_caches_successes(self) -> None:
        from translation_platform.live_smoke import LIVE_SMOKE_SAMPLES

        evidence, translator = self.run_smoke([self.success()] * 20)

        self.assertEqual(len(LIVE_SMOKE_SAMPLES), 20)
        self.assertEqual(len({sample.sample_id for sample in LIVE_SMOKE_SAMPLES}), 20)
        self.assertEqual(len(translator.calls), 20)
        self.assertEqual(evidence["status"], "available")
        self.assertEqual(evidence["planned_samples"], 20)
        self.assertEqual(evidence["actual_samples"], 20)
        self.assertEqual(evidence["request_count"], 20)
        self.assertEqual(evidence["cache_hits"], 0)
        self.assertEqual(evidence["successes"], 20)
        self.assertEqual(evidence["failures"], 0)
        self.assertEqual(evidence["success_rate"], 1.0)
        self.assertEqual(evidence["directions"]["zh-CHS_to_en"]["successes"], 10)
        self.assertEqual(evidence["directions"]["en_to_zh-CHS"]["successes"], 10)
        self.assertTrue(evidence["success_cache_ready"])

        records = self.checkpoint_store.load()
        self.assertEqual(len(records), 20)
        self.assertTrue(
            all(record.status is CheckpointStatus.SUCCESS for record in records.values())
        )
        first = LIVE_SMOKE_SAMPLES[0]
        first_key = build_cache_key(first.source_lang, first.target_lang, first.text)
        self.assertEqual(records[first_key].translated_text, "synthetic translated text")

    def test_degraded_runs_all_samples_and_aggregates_failures_and_retries(self) -> None:
        outcomes = [self.success()] * 17 + [
            self.failure(ErrorType.TIMEOUT, attempts=2, retries=1),
            self.failure(ErrorType.HTTP_5XX, attempts=2, retries=1),
            self.failure(ErrorType.EMPTY_RESULT),
        ]

        evidence, translator = self.run_smoke(outcomes)

        self.assertEqual(len(translator.calls), 20)
        self.assertEqual(evidence["status"], "degraded")
        self.assertEqual(evidence["successes"], 17)
        self.assertEqual(evidence["failures"], 3)
        self.assertEqual(evidence["success_rate"], 0.85)
        self.assertEqual(
            evidence["error_counts"],
            {"empty_result": 1, "http_5xx": 1, "timeout": 1},
        )
        self.assertEqual(evidence["request_attempts"], 22)
        self.assertEqual(evidence["retries"], 2)

    def test_same_systemic_probe_failure_stops_after_two_real_samples(self) -> None:
        evidence, translator = self.run_smoke(
            [
                self.failure(ErrorType.SIGNATURE_TOKEN),
                self.failure(ErrorType.SIGNATURE_TOKEN),
            ]
        )

        self.assertEqual(len(translator.calls), 2)
        self.assertEqual(evidence["status"], "unavailable")
        self.assertEqual(evidence["actual_samples"], 2)
        self.assertEqual(evidence["successes"], 0)
        self.assertEqual(evidence["failures"], 2)
        self.assertEqual(evidence["error_counts"], {"signature_token": 2})
        self.assertFalse(evidence["success_cache_ready"])
        statuses = [record.status for record in self.checkpoint_store.load().values()]
        self.assertEqual(statuses.count(CheckpointStatus.FAILURE), 2)
        self.assertEqual(statuses.count(CheckpointStatus.PENDING), 18)

    def test_resume_preserves_terminal_records_and_executes_only_unfinished(self) -> None:
        from translation_platform.live_smoke import LIVE_SMOKE_SAMPLES

        cached_success_sample = LIVE_SMOKE_SAMPLES[0]
        cached_failure_sample = LIVE_SMOKE_SAMPLES[10]
        pending_sample = LIVE_SMOKE_SAMPLES[1]
        cached_success = CheckpointRecord(
            cache_key=build_cache_key(
                cached_success_sample.source_lang,
                cached_success_sample.target_lang,
                cached_success_sample.text,
            ),
            status=CheckpointStatus.SUCCESS,
            attempts=3,
            translated_text="cached fixture translation",
            error_type=None,
            latency_ms=30.0,
        )
        cached_failure = CheckpointRecord(
            cache_key=build_cache_key(
                cached_failure_sample.source_lang,
                cached_failure_sample.target_lang,
                cached_failure_sample.text,
            ),
            status=CheckpointStatus.FAILURE,
            attempts=2,
            translated_text=None,
            error_type=ErrorType.TIMEOUT,
            latency_ms=20.0,
        )
        pending_key = build_cache_key(
            pending_sample.source_lang,
            pending_sample.target_lang,
            pending_sample.text,
        )
        self.checkpoint_store.save(
            {
                cached_success.cache_key: cached_success,
                cached_failure.cache_key: cached_failure,
                pending_key: CheckpointRecord(
                    cache_key=pending_key,
                    status=CheckpointStatus.PENDING,
                    attempts=0,
                    translated_text=None,
                    error_type=None,
                    latency_ms=0.0,
                ),
            }
        )

        evidence, translator = self.run_smoke(
            [self.success(attempts=2, retries=1)] + [self.success()] * 17
        )

        self.assertEqual(len(translator.calls), 18)
        self.assertNotIn(cached_success_sample.sample_id, translator.calls)
        self.assertNotIn(cached_failure_sample.sample_id, translator.calls)
        self.assertEqual(evidence["request_count"], 18)
        self.assertEqual(evidence["cache_hits"], 2)
        self.assertEqual(evidence["request_attempts"], 19)
        self.assertEqual(evidence["retries"], 1)
        self.assertEqual(evidence["actual_samples"], 20)
        self.assertEqual(evidence["successes"], 19)
        self.assertEqual(evidence["failures"], 1)
        self.assertEqual(evidence["success_rate"], 0.95)
        self.assertEqual(evidence["error_counts"], {"timeout": 1})
        self.assertEqual(evidence["directions"]["zh-CHS_to_en"]["successes"], 10)
        self.assertEqual(evidence["directions"]["en_to_zh-CHS"]["successes"], 9)
        records = self.checkpoint_store.load()
        self.assertEqual(records[cached_success.cache_key], cached_success)
        self.assertEqual(records[cached_failure.cache_key], cached_failure)

    def test_complete_checkpoint_rerun_is_twenty_cache_hits_and_zero_requests(self) -> None:
        from translation_platform.live_smoke import LIVE_SMOKE_SAMPLES

        records = {}
        for sample in LIVE_SMOKE_SAMPLES:
            cache_key = build_cache_key(
                sample.source_lang,
                sample.target_lang,
                sample.text,
            )
            records[cache_key] = CheckpointRecord(
                cache_key=cache_key,
                status=CheckpointStatus.SUCCESS,
                attempts=1,
                translated_text="cached fixture translation",
                error_type=None,
                latency_ms=10.0,
            )
        self.checkpoint_store.save(records)

        evidence, translator = self.run_smoke([])

        self.assertEqual(translator.calls, [])
        self.assertEqual(evidence["status"], "available")
        self.assertEqual(evidence["actual_samples"], 20)
        self.assertEqual(evidence["successes"], 20)
        self.assertEqual(evidence["request_count"], 0)
        self.assertEqual(evidence["cache_hits"], 20)
        self.assertEqual(evidence["request_attempts"], 0)
        self.assertEqual(evidence["retries"], 0)

    def test_cached_systemic_probe_failures_stop_before_pending_requests(self) -> None:
        from translation_platform.live_smoke import LIVE_SMOKE_SAMPLES

        records = {}
        probe_keys = []
        for index, sample in enumerate(LIVE_SMOKE_SAMPLES):
            cache_key = build_cache_key(sample.source_lang, sample.target_lang, sample.text)
            is_probe = index in (0, 10)
            records[cache_key] = CheckpointRecord(
                cache_key=cache_key,
                status=(
                    CheckpointStatus.FAILURE if is_probe else CheckpointStatus.PENDING
                ),
                attempts=1 if is_probe else 0,
                translated_text=None,
                error_type=ErrorType.SIGNATURE_TOKEN if is_probe else None,
                latency_ms=5.0 if is_probe else 0.0,
            )
            if is_probe:
                probe_keys.append(cache_key)
        self.checkpoint_store.save(records)

        evidence, translator = self.run_smoke([])

        self.assertEqual(translator.calls, [])
        self.assertEqual(evidence["status"], "unavailable")
        self.assertEqual(evidence["actual_samples"], 2)
        self.assertEqual(evidence["request_count"], 0)
        self.assertEqual(evidence["cache_hits"], 2)
        self.assertEqual(evidence["request_attempts"], 0)
        self.assertEqual(evidence["retries"], 0)
        self.assertEqual(evidence["error_counts"], {"signature_token": 2})
        after = self.checkpoint_store.load()
        self.assertTrue(
            all(after[key].status is CheckpointStatus.FAILURE for key in probe_keys)
        )
        self.assertEqual(
            sum(record.status is CheckpointStatus.PENDING for record in after.values()),
            18,
        )

    def test_different_probe_failures_do_not_trigger_systemic_early_stop(self) -> None:
        outcomes = [
            self.failure(ErrorType.NETWORK_CONNECTION),
            self.failure(ErrorType.RESPONSE_FORMAT),
        ] + [self.success()] * 18

        evidence, translator = self.run_smoke(outcomes)

        self.assertEqual(len(translator.calls), 20)
        self.assertEqual(evidence["status"], "available")
        self.assertEqual(evidence["successes"], 18)
        self.assertEqual(evidence["failures"], 2)

    def test_evidence_contains_only_hash_summaries_not_inputs_outputs_or_messages(self) -> None:
        from translation_platform.live_smoke import LIVE_SMOKE_SAMPLES

        sensitive_output = "fixture translated output that must stay private"
        evidence, _ = self.run_smoke([self.success(sensitive_output)] * 20)
        serialized = json.dumps(evidence, ensure_ascii=False)

        self.assertEqual(evidence["evidence_type"], "live_low_load_smoke")
        self.assertLessEqual(len(evidence["sample_results"]), 2)
        self.assertNotIn(sensitive_output, serialized)
        for sample in LIVE_SMOKE_SAMPLES:
            self.assertNotIn(sample.text, serialized)
        for forbidden_key in (
            "endpoint",
            "profile",
            "token",
            "cookie",
            "response",
            "error_message",
        ):
            self.assertNotIn(forbidden_key, serialized.lower())
        self.assertTrue(
            all(
                result["input_summary"].startswith("sha256:")
                for result in evidence["sample_results"]
            )
        )

    def test_completed_success_survives_interruption_and_next_item_stays_pending(self) -> None:
        from translation_platform.live_smoke import LIVE_SMOKE_SAMPLES, LiveSmokeRunner

        translator = SequenceTranslator([self.success("cached result"), KeyboardInterrupt()])
        runner = LiveSmokeRunner(
            translate=translator,
            checkpoint_store=self.checkpoint_store,
            now=lambda: self.started_at,
            proxy_enabled=False,
        )

        with self.assertRaises(KeyboardInterrupt):
            runner.run()

        records = self.checkpoint_store.load()
        first, second = LIVE_SMOKE_SAMPLES[0], LIVE_SMOKE_SAMPLES[10]
        first_key = build_cache_key(first.source_lang, first.target_lang, first.text)
        second_key = build_cache_key(second.source_lang, second.target_lang, second.text)
        self.assertEqual(records[first_key].status, CheckpointStatus.SUCCESS)
        self.assertEqual(records[first_key].translated_text, "cached result")
        self.assertEqual(records[second_key].status, CheckpointStatus.PENDING)

    def test_fixed_samples_cover_both_directions_numbers_and_punctuation(self) -> None:
        from translation_platform.live_smoke import LIVE_SMOKE_SAMPLES

        directions = [
            (sample.source_lang, sample.target_lang) for sample in LIVE_SMOKE_SAMPLES
        ]
        self.assertEqual(directions.count(("zh-CHS", "en")), 10)
        self.assertEqual(directions.count(("en", "zh-CHS")), 10)
        self.assertTrue(any(any(character.isdigit() for character in sample.text) for sample in LIVE_SMOKE_SAMPLES))
        self.assertTrue(any(any(character in "，。！？,.!?" for character in sample.text) for sample in LIVE_SMOKE_SAMPLES))


if __name__ == "__main__":
    unittest.main()
