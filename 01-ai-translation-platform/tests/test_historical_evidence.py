"""历史工作簿核对与脱敏聚合证据的合成行为测试。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from scripts.audit_history import (
    build_historical_evidence,
    reconcile_history,
    write_historical_evidence,
)


class HistoricalEvidenceTests(unittest.TestCase):
    """核对必须只使用日志位置，并且证据不得泄漏合成单元格内容。"""

    def _write_workbook(self, path: Path) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "合成工作表"
        sheet.append(["目标列", "其他列"])
        sheet.append(["合成结果甲", "无关值"])
        # 期望结果故意出现在错误列，核对仍必须认定目标坐标不匹配。
        sheet.append(["实际不同", "合成结果乙"])
        workbook.save(path)

    def _write_logs(self, directory: Path) -> tuple[Path, Path]:
        translation_log = directory / "translation.txt"
        write_log = directory / "write.txt"
        translation_log.write_text(
            "[2026-01-02 03:04:05] : 「合成输入甲」→「合成结果甲」\n"
            "[2026-01-02 03:04:06] : 「合成输入甲」→「合成结果甲」\n"
            "[2026-01-02 03:04:07] : 翻译失败\n"
            "不可解析的合成记录\n",
            encoding="utf-8",
        )
        write_log.write_text(
            "[2026-01-02 03:05:01] 1行“目标列”列: 「合成输入甲」→「合成结果甲」\n"
            "[2026-01-02 03:05:02] 1行“目标列”列: 「合成输入甲」→「合成结果甲」\n"
            "[2026-01-02 03:05:03] 2行“目标列”列: 「合成输入乙」→「合成结果乙」\n"
            "[2026-01-02 03:05:04] : 「合成输入丙」→「合成结果丙」\n"
            "[2026-01-02 03:05:05] 1行“不存在列”列: 「合成输入丁」→「合成结果丁」\n"
            "[2026-01-02 03:05:06] 99行“目标列”列: 「合成输入戊」→「合成结果戊」\n",
            encoding="utf-8",
        )
        return translation_log, write_log

    def test_reconciliation_counts_matches_mismatches_duplicates_and_unlocatable_records(self) -> None:
        """若错误地按全表查找、忽略重复或猜测位置，人工核对的计数会改变。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            workbook_path = directory / "table.xlsx"
            self._write_workbook(workbook_path)
            translation_log, write_log = self._write_logs(directory)
            before_hash = hashlib.sha256(workbook_path.read_bytes()).hexdigest()

            evidence = reconcile_history(translation_log, write_log, workbook_path)

            self.assertEqual(hashlib.sha256(workbook_path.read_bytes()).hexdigest(), before_hash)
            self.assertEqual(evidence["logs"]["translation_log"]["total_lines"], 4)
            self.assertEqual(evidence["logs"]["translation_log"]["successful_lines"], 2)
            self.assertEqual(evidence["logs"]["translation_log"]["duplicates"], 1)
            self.assertEqual(
                evidence["statistical_basis"]["log_unit"],
                "日志物理行，包含空白轮次分隔行；日志行数不等于唯一翻译数。",
            )
            self.assertEqual(evidence["workbook_reconciliation"], {
                "successful_write_records": 6,
                "verifiable_write_records": 3,
                "successful_matches": 2,
                "mismatches": 1,
                "unlocatable_records": 3,
                "unique_successful_write_cells": 1,
            })
            self.assertFalse(evidence["threshold"]["is_met"])
            serialized = json.dumps(evidence, ensure_ascii=False)
            self.assertNotIn("合成输入", serialized)
            self.assertNotIn("合成结果", serialized)
            self.assertNotIn("目标列", serialized)

    def test_missing_worksheet_is_counted_as_unlocatable_without_guessing(self) -> None:
        """若指定工作表不存在仍改用别的表，位置级核对会被错误放宽。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            workbook_path = directory / "table.xlsx"
            self._write_workbook(workbook_path)
            translation_log, write_log = self._write_logs(directory)

            evidence = reconcile_history(
                translation_log,
                write_log,
                workbook_path,
                workbook_sheet="不存在工作表",
            )

            self.assertEqual(evidence["workbook_reconciliation"]["verifiable_write_records"], 0)
            self.assertEqual(evidence["workbook_reconciliation"]["unlocatable_records"], 6)
            self.assertEqual(evidence["workbook_reconciliation"]["successful_matches"], 0)

    def test_atomic_write_flushes_and_fsyncs_before_replacing_evidence(self) -> None:
        """若替换先于持久写入，断电时可能得到已替换却不完整的证据。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "historical_evidence.json"
            evidence = build_historical_evidence(
                translation_summary={"total_lines": 0},
                write_summary={"total_lines": 0},
                successful_write_records=0,
                verifiable_write_records=0,
                successful_matches=0,
                mismatches=0,
                unlocatable_records=0,
                unique_successful_write_cells=0,
            )
            events: list[str] = []
            real_named_temporary_file = tempfile.NamedTemporaryFile
            real_fsync = os.fsync
            real_replace = os.replace

            class TrackingTemporaryFile:
                def __init__(self, context: object) -> None:
                    self._context = context
                    self._handle: object | None = None

                def __enter__(self) -> object:
                    self._handle = self._context.__enter__()  # type: ignore[attr-defined]
                    return self

                def __exit__(self, *arguments: object) -> object:
                    return self._context.__exit__(*arguments)  # type: ignore[attr-defined]

                def write(self, text: str) -> int:
                    return self._handle.write(text)  # type: ignore[union-attr]

                def flush(self) -> None:
                    events.append("flush")
                    self._handle.flush()  # type: ignore[union-attr]

                def fileno(self) -> int:
                    return self._handle.fileno()  # type: ignore[union-attr]

                @property
                def name(self) -> str:
                    return self._handle.name  # type: ignore[union-attr]

            def named_temporary_file(*arguments: object, **keywords: object) -> TrackingTemporaryFile:
                return TrackingTemporaryFile(real_named_temporary_file(*arguments, **keywords))

            def fsync(file_descriptor: int) -> None:
                events.append("fsync")
                real_fsync(file_descriptor)

            def replace(source: object, target: object) -> None:
                events.append("replace")
                real_replace(source, target)

            with patch("scripts.audit_history.tempfile.NamedTemporaryFile", side_effect=named_temporary_file), patch(
                "scripts.audit_history.os.fsync", side_effect=fsync
            ), patch("scripts.audit_history.os.replace", side_effect=replace):
                write_historical_evidence(evidence, output_path)

            self.assertEqual(events, ["flush", "fsync", "replace"])
            self.assertTrue(output_path.is_file())

    def test_fsync_failure_preserves_existing_evidence_and_removes_temporary_file(self) -> None:
        """若替换前持久写入失败仍替换或遗留临时文件，旧证据将不再可靠。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_path = Path(temporary_directory) / "historical_evidence.json"
            output_path.write_text('{"旧证据": true}\n', encoding="utf-8")
            evidence = build_historical_evidence(
                translation_summary={"total_lines": 0},
                write_summary={"total_lines": 0},
                successful_write_records=0,
                verifiable_write_records=0,
                successful_matches=0,
                mismatches=0,
                unlocatable_records=0,
                unique_successful_write_cells=0,
            )

            with patch("scripts.audit_history.os.fsync", side_effect=OSError("模拟失败")):
                with self.assertRaises(OSError):
                    write_historical_evidence(evidence, output_path)

            self.assertEqual(output_path.read_text(encoding="utf-8"), '{"旧证据": true}\n')
            self.assertEqual(list(output_path.parent.glob(f".{output_path.name}.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
