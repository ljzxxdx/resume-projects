"""历史日志审计的合成行为测试。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
import tempfile
from pathlib import Path

from scripts.audit_history import (
    audit_file,
    audit_history,
    audit_log_lines,
    successful_records,
)


class AuditHistoryTests(unittest.TestCase):
    """审计必须稳定统计有效、失败、重复和无法解析的合成记录。"""

    def test_audit_log_lines_counts_mixed_records_and_rounds(self) -> None:
        """删除任一统计分支时，混合合成记录的人工期望值必须不匹配。"""
        lines = (
            "[2026-01-02 03:04:05] : 「合成输入甲」→「synthetic:甲」",
            "[2026-01-02 03:04:06] : 翻译失败",
            "这是一条无法解析的合成记录",
            "",
            "[2026-01-02 03:05:05] 7行“合成列”: 「合成输入甲」→「synthetic:甲」",
            "[2026-01-02 03:05:06] : 「合成输入乙」→「synthetic:乙」",
        )

        summary = audit_log_lines(lines)

        self.assertEqual(summary.total_lines, 6)
        self.assertEqual(summary.successful_lines, 3)
        self.assertEqual(summary.failed_lines, 1)
        self.assertEqual(summary.unique_inputs, 2)
        self.assertEqual(summary.unique_input_result_pairs, 2)
        self.assertEqual(summary.duplicates, 1)
        self.assertEqual(summary.unparseable_lines, 1)
        self.assertEqual(
            [round_summary.to_dict() for round_summary in summary.rounds],
            [
                {
                    "round": 1,
                    "total_lines": 3,
                    "successful_lines": 1,
                    "failed_lines": 1,
                    "unparseable_lines": 1,
                },
                {
                    "round": 2,
                    "total_lines": 2,
                    "successful_lines": 2,
                    "failed_lines": 0,
                    "unparseable_lines": 0,
                },
            ],
        )

    def test_successful_records_exposes_pairs_for_later_reconciliation(self) -> None:
        """若未暴露解析记录，后续工作簿核对无法复用已验证的日志语法。"""
        records = successful_records(
            (
                "[2026-01-02 03:04:05] : 「合成甲」→「synthetic:a」",
                "",
                "[2026-01-02 03:05:05] 2行“合成列”: 「合成乙」→「synthetic:b」",
                "[2026-01-02 03:05:06] : 翻译失败",
            )
        )

        self.assertEqual(
            [(record.round, record.input_text, record.result_text) for record in records],
            [
                (1, "合成甲", "synthetic:a"),
                (2, "合成乙", "synthetic:b"),
            ],
        )

    def test_audit_history_reads_two_files_without_changing_synthetic_content(self) -> None:
        """文件审计若漏读任一日志或发生写入，合成夹具的结果必须失败。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            translation_log = directory / "translation.txt"
            write_log = directory / "write.txt"
            translation_content = "[2026-01-02 03:04:05] : 「合成甲」→「synthetic:a」\n"
            write_content = (
                "[2026-01-02 03:04:05] 1行“合成列”: 「合成甲」→「synthetic:a」\n"
                "[2026-01-02 03:04:06] 2行“合成列”: 「合成乙」→「synthetic:b」\n"
            )
            translation_log.write_text(translation_content, encoding="utf-8")
            write_log.write_text(write_content, encoding="utf-8")

            report = audit_history(translation_log, write_log)

            self.assertEqual(translation_log.read_text(encoding="utf-8"), translation_content)
            self.assertEqual(write_log.read_text(encoding="utf-8"), write_content)
            self.assertEqual(report.translation_log.successful_lines, 1)
            self.assertEqual(report.write_log.successful_lines, 2)
            self.assertEqual(report.write_log.unique_inputs, 2)

    def test_empty_and_missing_log_files_have_deterministic_behavior(self) -> None:
        """空文件或缺失文件未被明确处理时，调用方无法可靠审计历史记录。"""
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            empty_log = directory / "empty.txt"
            missing_log = directory / "missing.txt"
            empty_log.write_text("", encoding="utf-8")

            summary = audit_file(empty_log)

            self.assertEqual(summary.total_lines, 0)
            self.assertEqual(summary.successful_lines, 0)
            self.assertEqual(summary.failed_lines, 0)
            self.assertEqual(summary.unparseable_lines, 0)
            self.assertEqual(summary.rounds, ())
            with self.assertRaises(FileNotFoundError):
                audit_file(missing_log)

    def test_cli_outputs_json_and_rejects_missing_synthetic_file(self) -> None:
        """CLI 若未输出 JSON 或未校验文件，自动化调用会得到不可靠结果。"""
        project_root = Path(__file__).resolve().parents[1]
        script = project_root / "scripts" / "audit_history.py"
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            translation_log = directory / "translation.txt"
            write_log = directory / "write.txt"
            translation_log.write_text(
                "[2026-01-02 03:04:05] : 「合成甲」→「synthetic:a」\n",
                encoding="utf-8",
            )
            write_log.write_text(
                "[2026-01-02 03:04:06] 1行“合成列”: 「合成乙」→「synthetic:b」\n",
                encoding="utf-8",
            )

            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--translation-log",
                    str(translation_log),
                    "--write-log",
                    str(write_log),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                check=False,
            )
            missing = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--translation-log",
                    str(directory / "missing.txt"),
                    "--write-log",
                    str(write_log),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=environment,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(completed.stdout.strip(), "CLI 未输出 JSON 审计结果")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["translation_log"]["successful_lines"], 1)
        self.assertEqual(payload["write_log"]["unique_inputs"], 1)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("翻译日志文件不存在", missing.stderr)


if __name__ == "__main__":
    unittest.main()
