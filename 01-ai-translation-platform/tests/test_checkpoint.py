from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from translation_platform.errors import ErrorType


class JsonlCheckpointStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "checkpoint.jsonl"

    def test_all_checkpoint_statuses_round_trip_without_sensitive_fields(self) -> None:
        from translation_platform.checkpoint import (
            CheckpointRecord,
            CheckpointStatus,
            JsonlCheckpointStore,
        )

        records = {
            "sha256:pending": CheckpointRecord(
                cache_key="sha256:pending",
                status=CheckpointStatus.PENDING,
                attempts=0,
                translated_text=None,
                error_type=None,
                latency_ms=0,
            ),
            "sha256:success": CheckpointRecord(
                cache_key="sha256:success",
                status=CheckpointStatus.SUCCESS,
                attempts=1,
                translated_text="合成翻译",
                error_type=None,
                latency_ms=12.5,
            ),
            "sha256:failure": CheckpointRecord(
                cache_key="sha256:failure",
                status=CheckpointStatus.FAILURE,
                attempts=2,
                translated_text=None,
                error_type=ErrorType.TIMEOUT,
                latency_ms=30,
            ),
        }

        store = JsonlCheckpointStore(self.path)
        store.save(records)

        self.assertEqual(store.load(), records)
        serialized_records = [
            json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(serialized_records), 3)
        for serialized_record in serialized_records:
            with self.subTest(cache_key=serialized_record["cache_key"]):
                self.assertEqual(
                    set(serialized_record),
                    {
                        "cache_key",
                        "status",
                        "attempts",
                        "translated_text",
                        "error_type",
                        "latency_ms",
                    },
                )
                self.assertTrue(
                    {"source_text", "proxy", "token", "response", "input_path"}
                    .isdisjoint(serialized_record)
                )

    def test_load_rejects_malformed_json_and_invalid_fields(self) -> None:
        from translation_platform.checkpoint import (
            CheckpointFormatError,
            JsonlCheckpointStore,
        )

        store = JsonlCheckpointStore(self.path)
        invalid_lines = (
            "{broken json}",
            (
                '{"cache_key":"sha256:first","cache_key":"sha256:second",'
                '"status":"pending","attempts":0,"translated_text":null,'
                '"error_type":null,"latency_ms":0}'
            ),
            json.dumps(
                {
                    "cache_key": "sha256:bad",
                    "status": "success",
                    "attempts": 1,
                    "translated_text": "合成翻译",
                    "error_type": None,
                    "latency_ms": 1,
                    "source_text": "不得保存",
                }
            ),
            json.dumps(
                {
                    "cache_key": "sha256:bad",
                    "status": "success",
                    "attempts": 1,
                    "translated_text": None,
                    "error_type": None,
                    "latency_ms": 1,
                }
            ),
            json.dumps(
                {
                    "cache_key": "sha256:bad",
                    "status": "failure",
                    "attempts": 1,
                    "translated_text": None,
                    "error_type": "unknown",
                    "latency_ms": 1,
                }
            ),
        )

        for invalid_line in invalid_lines:
            with self.subTest(invalid_line=invalid_line):
                self.path.write_text(invalid_line + "\n", encoding="utf-8")
                with self.assertRaisesRegex(CheckpointFormatError, "第 1 行"):
                    store.load()

    def test_load_uses_last_record_for_duplicate_cache_key(self) -> None:
        from translation_platform.checkpoint import (
            CheckpointStatus,
            JsonlCheckpointStore,
        )

        self.path.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "cache_key": "sha256:duplicate",
                            "status": "pending",
                            "attempts": 0,
                            "translated_text": None,
                            "error_type": None,
                            "latency_ms": 0,
                        }
                    ),
                    json.dumps(
                        {
                            "cache_key": "sha256:duplicate",
                            "status": "failure",
                            "attempts": 1,
                            "translated_text": None,
                            "error_type": "timeout",
                            "latency_ms": 25,
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )

        record = JsonlCheckpointStore(self.path).load()["sha256:duplicate"]

        self.assertEqual(record.status, CheckpointStatus.FAILURE)
        self.assertEqual(record.attempts, 1)
        self.assertEqual(record.error_type, ErrorType.TIMEOUT)

    def test_failed_atomic_replacement_preserves_existing_checkpoint(self) -> None:
        from translation_platform.checkpoint import (
            CheckpointRecord,
            CheckpointStatus,
            JsonlCheckpointStore,
        )

        store = JsonlCheckpointStore(self.path)
        original_records = {
            "sha256:old": CheckpointRecord(
                cache_key="sha256:old",
                status=CheckpointStatus.PENDING,
                attempts=0,
                translated_text=None,
                error_type=None,
                latency_ms=0,
            )
        }
        replacement_records = {
            "sha256:new": CheckpointRecord(
                cache_key="sha256:new",
                status=CheckpointStatus.SUCCESS,
                attempts=1,
                translated_text="合成翻译",
                error_type=None,
                latency_ms=5,
            )
        }
        store.save(original_records)
        original_bytes = self.path.read_bytes()

        with patch(
            "translation_platform.checkpoint.os.replace",
            side_effect=OSError("synthetic replacement failure"),
        ):
            with self.assertRaisesRegex(OSError, "synthetic replacement failure"):
                store.save(replacement_records)

        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(store.load(), original_records)


if __name__ == "__main__":
    unittest.main()
