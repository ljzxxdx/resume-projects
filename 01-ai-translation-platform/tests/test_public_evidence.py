"""公开人工合成样例的离线安全边界测试。"""

from __future__ import annotations

import csv
import json
import re
import unittest
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from translation_platform.observability import BatchRunSummary


EVIDENCE_DIRECTORY = Path(__file__).resolve().parents[1] / "artifacts" / "public-evidence"
SAMPLE_FILES = (
    EVIDENCE_DIRECTORY / "sample_input.csv",
    EVIDENCE_DIRECTORY / "sample_output.csv",
    EVIDENCE_DIRECTORY / "run_summary.json",
    EVIDENCE_DIRECTORY / "README.md",
)

SENSITIVE_MARKER_PATTERNS = (
    ("network_url", re.compile(r"(?i)\b(?:https?|socks[45]?)://\S+")),
    (
        "url_userinfo",
        re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@"),
    ),
    (
        "authorization_header",
        re.compile(
            r"(?im)^\s*(?:authorization|proxy-authorization)\s*:\s*\S+"
        ),
    ),
    (
        "credential_key",
        re.compile(
            r"(?i)\b(?:cookie|token|access[_-]?token|auth[_-]?token|"
            r"refresh[_-]?token|api[_-]?key|client[_-]?secret|"
            r"password|secret|credential)\b\s*(?:=|:)\s*\S+"
        ),
    ),
    ("ipv4_host_port", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b")),
    (
        "domain_host_port",
        re.compile(r"(?i)\b(?:[a-z0-9-]+\.)+[a-z]{2,63}:\d{1,5}\b"),
    ),
    ("windows_drive_path", re.compile(r"(?i)\b[a-z]:[\\/]")),
    (
        "unc_path",
        re.compile(r"(?<![A-Za-z0-9])(?:\\\\|//)[^\\/\s]+[\\/][^\\/\s]+"),
    ),
    (
        "posix_path",
        re.compile(
            r"(?i)(?<![A-Za-z0-9:])/"
            r"(?:etc|home|users|private|var|tmp|srv|opt|usr|root|mnt|volumes)(?:/|$)"
        ),
    ),
    ("private_artifact_path", re.compile(r"(?i)artifacts[\\/]private")),
)


def _find_sensitive_markers(content: str) -> tuple[str, ...]:
    """返回内容中命中的通用敏感类别，不包含供应商专属规则。"""
    return tuple(
        category
        for category, pattern in SENSITIVE_MARKER_PATTERNS
        if pattern.search(content) is not None
    )


class PublicEvidenceTests(unittest.TestCase):
    """样例必须是可公开、可复核且明显离线合成的证据。"""

    def test_evidence_files_exist_and_contain_no_private_markers(self) -> None:
        """缺少样例或泄露私有标记时，公开证据校验必须失败。"""
        for path in SAMPLE_FILES:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), "公开样例文件缺失")
                content = path.read_text(encoding="utf-8")
                self.assertEqual(_find_sensitive_markers(content), ())

    def test_sensitive_content_scanner_rejects_generic_marker_categories(self) -> None:
        """放宽任一通用敏感类别规则时，合成探针必须失败。"""
        probes = (
            ("network_url", "https://relay.invalid:8443/fixture"),
            ("network_url", "socks5://relay.invalid:1080"),
            ("url_userinfo", "demo://fixture-user:fixture-secret@relay.invalid:8443"),
            ("authorization_header", "Authorization: fixture-value"),
            ("authorization_header", "Proxy-Authorization: fixture-value"),
            ("credential_key", "refresh_token=fixture-value"),
            ("credential_key", "token=fixture-value"),
            ("credential_key", "client_secret: fixture-value"),
            ("ipv4_host_port", "192.0.2.1:1080"),
            ("domain_host_port", "relay.invalid:1080"),
            ("windows_drive_path", r"C:\fixture\sample.csv"),
            ("windows_drive_path", "C:/fixture/sample.csv"),
            ("unc_path", r"\\host.invalid\share\sample.csv"),
            ("posix_path", "/etc/fixture.conf"),
            ("posix_path", "/home/fixture/sample.csv"),
            ("posix_path", "/Users/fixture/sample.csv"),
            ("posix_path", "/opt/fixture/sample.csv"),
            ("posix_path", "/usr/fixture/sample.csv"),
            ("posix_path", "/root/fixture/sample.csv"),
            ("posix_path", "/mnt/fixture/sample.csv"),
            ("posix_path", "/Volumes/fixture/sample.csv"),
            ("private_artifact_path", "artifacts/private/fixture.txt"),
        )

        for expected_category, content in probes:
            with self.subTest(expected_category=expected_category, content=content):
                self.assertIn(expected_category, _find_sensitive_markers(content))

    def test_csv_summary_and_readme_use_one_derived_fixture_accounting(self) -> None:
        """去重、执行结果、摘要和 README 的数量口径漂移时必须失败。"""
        with (EVIDENCE_DIRECTORY / "sample_input.csv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            inputs = list(csv.DictReader(handle))
        with (EVIDENCE_DIRECTORY / "sample_output.csv").open(
            "r", encoding="utf-8", newline=""
        ) as handle:
            outputs = list(csv.DictReader(handle))
        summary = json.loads(
            (EVIDENCE_DIRECTORY / "run_summary.json").read_text(encoding="utf-8")
        )
        readme = (EVIDENCE_DIRECTORY / "README.md").read_text(encoding="utf-8")

        self.assertGreaterEqual(len(inputs), 3)
        source_texts = [row["source_text"] for row in inputs]
        self.assertLess(len(set(source_texts)), len(source_texts))
        synthetic_marker = re.compile(r"(?i)(合成|示例|synthetic|example)")
        self.assertTrue(all(synthetic_marker.search(text) for text in source_texts))
        self.assertEqual(len(outputs), len(inputs))
        self.assertEqual(
            [row["record_id"] for row in outputs],
            [row["record_id"] for row in inputs],
        )
        self.assertTrue(all("synthetic:" in row["translated_text"] for row in outputs))
        self.assertTrue(
            all(row["fixture"] == "deterministic-fake-translator" for row in outputs)
        )

        input_by_id = {row["record_id"]: row for row in inputs}
        for output in outputs:
            self.assertEqual(
                output["source_text"],
                input_by_id[output["record_id"]]["source_text"],
            )
        unique_tasks = {
            (row["source_lang"], row["target_lang"], row["source_text"])
            for row in inputs
        }
        execution_counts = {
            status: sum(row["execution_result"] == status for row in outputs)
            for status in ("executed", "reused-duplicate")
        }
        self.assertEqual(execution_counts["executed"], len(unique_tasks))
        self.assertEqual(
            execution_counts["reused-duplicate"], len(inputs) - len(unique_tasks)
        )

        grouped_outputs = {}
        for row in outputs:
            grouped_outputs.setdefault(row["duplicate_group"], []).append(row)
        duplicate_rows = 0
        for rows in grouped_outputs.values():
            if len(rows) > 1:
                duplicate_rows += len(rows) - 1
                self.assertEqual(
                    {input_by_id[row["record_id"]]["source_text"] for row in rows},
                    {input_by_id[rows[0]["record_id"]]["source_text"]},
                )
                self.assertEqual(
                    {row["translated_text"] for row in rows},
                    {rows[0]["translated_text"]},
                )
        self.assertEqual(duplicate_rows, execution_counts["reused-duplicate"])

        self.assertEqual(summary["input_cells"], len(inputs))
        self.assertEqual(summary["unique_texts"], len(unique_tasks))
        self.assertEqual(summary["successes"], execution_counts["executed"])
        self.assertEqual(summary["failures"], 0)
        self.assertEqual(summary["cache_hits"], 0)
        self.assertEqual(summary["retries"], 0)
        self.assertEqual(summary["error_counts"], {})
        for expected_text in (
            f"{len(inputs)} 个输入单元",
            f"{len(unique_tasks)} 个唯一文本",
            f"{summary['successes']} 个成功结果",
            f"{summary['failures']} 个失败结果",
            f"{summary['cache_hits']} 个缓存命中",
            f"{summary['retries']} 次重试",
        ):
            with self.subTest(expected_text=expected_text):
                self.assertIn(expected_text, readme)
        self.assertIn("重复文本", readme)
        self.assertIn("去重后", readme)

    def test_summary_uses_only_task_seven_fields_with_aware_timestamps(self) -> None:
        """摘要字段漂移、非闭合结构或无时区时间戳应被拒绝。"""
        payload = json.loads(
            (EVIDENCE_DIRECTORY / "run_summary.json").read_text(encoding="utf-8")
        )
        expected_fields = {field.name for field in fields(BatchRunSummary)}

        self.assertEqual(set(payload), expected_fields)
        self.assertIsNotNone(datetime.fromisoformat(payload["started_at"]).tzinfo)
        self.assertIsNotNone(datetime.fromisoformat(payload["finished_at"]).tzinfo)
        BatchRunSummary(**payload)

    def test_readme_explains_offline_synthetic_evidence_limits(self) -> None:
        """删除离线、合成、非实测或去重说明时，读者会被误导。"""
        readme = (EVIDENCE_DIRECTORY / "README.md").read_text(encoding="utf-8")

        for required_text in (
            "离线 mock 合成证据",
            "不代表当前接口实测或真实调用次数",
            "确定性假翻译器",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, readme)

    def test_readme_defines_summary_accounting_boundaries(self) -> None:
        """摘要必须区分检查点命中、最终结果和仅本轮执行的指标。"""

        readme = (EVIDENCE_DIRECTORY / "README.md").read_text(encoding="utf-8")

        for required_text in (
            "cache_hits 包含未进入处理器的成功与失败 checkpoint",
            "失败 checkpoint 属于负缓存",
            "request_count + cache_hits == unique_texts",
            "successes、failures、error_counts 包含缓存的最终结果",
            "retries、average_latency_ms、p95_latency_ms 只统计本轮 executed keys",
            "全缓存时 retries、average_latency_ms、p95_latency_ms 均为 0",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, readme)


if __name__ == "__main__":
    unittest.main()
