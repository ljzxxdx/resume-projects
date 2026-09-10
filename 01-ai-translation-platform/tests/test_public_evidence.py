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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIRECTORY = PROJECT_ROOT / "artifacts" / "public-evidence"
VALIDATION_DIRECTORY = PROJECT_ROOT / "artifacts" / "validation"
ROOT_README = PROJECT_ROOT / "README.md"
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

POSITIVE_ONLINE_CONCLUSIONS = (
    "是可用服务",
    "接口可用",
    "90% 达标",
)
NEGATIVE_CLAIM_CONTEXT = re.compile(
    r"(?:不|未|非|无|不能|无法|不得|没有|不代表).{0,8}$"
)


def _find_sensitive_markers(content: str) -> tuple[str, ...]:
    """返回内容中命中的通用敏感类别，不包含供应商专属规则。"""
    return tuple(
        category
        for category, pattern in SENSITIVE_MARKER_PATTERNS
        if pattern.search(content) is not None
    )


def _read_validation_evidence(filename: str) -> dict:
    """读取一份公开安全的聚合验证证据。"""
    return json.loads(
        (VALIDATION_DIRECTORY / filename).read_text(encoding="utf-8")
    )


def _markdown_section(content: str, title: str) -> str:
    """按三级标题提取正文，避免用其他章节的数字满足断言。"""
    pattern = re.compile(
        rf"(?ms)^###\s+{re.escape(title)}\s*\n(.*?)(?=^#{{1,3}}\s|\Z)"
    )
    matches = pattern.findall(content)
    if len(matches) != 1:
        raise AssertionError(f"README 章节缺失或重复：{title}")
    return matches[0]


def _section_line(section: str, prefix: str) -> str:
    """提取证据章节中的单个统计行。"""
    matches = [line for line in section.splitlines() if line.startswith(prefix)]
    if len(matches) != 1:
        raise AssertionError(f"README 统计行缺失或重复：{prefix}")
    return matches[0]


def _find_positive_online_conclusions(content: str) -> tuple[str, ...]:
    """拒绝正向在线结论，同时允许紧邻明确否定语境的同一短语。"""
    findings = []
    for phrase in POSITIVE_ONLINE_CONCLUSIONS:
        for match in re.finditer(re.escape(phrase), content):
            context = content[max(0, match.start() - 12) : match.start()]
            if NEGATIVE_CLAIM_CONTEXT.search(context) is None:
                findings.append(phrase)
    return tuple(findings)


class PublicEvidenceTests(unittest.TestCase):
    """样例必须是可公开、可复核且明显离线合成的证据。"""

    def test_evidence_files_exist_and_contain_no_private_markers(self) -> None:
        """缺少样例或泄露私有标记时，公开证据校验必须失败。"""
        for path in SAMPLE_FILES:
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file(), "公开样例文件缺失")
                content = path.read_text(encoding="utf-8")
                self.assertEqual(_find_sensitive_markers(content), ())

    def _assert_root_readme_contract(self, readme: str) -> None:
        """核对 README 的在线边界与三个证据章节。"""
        for required_text in (
            "当前只完成参数校验",
            "未装配为公开的一键真实翻译",
        ):
            with self.subTest(required_text=required_text):
                self.assertIn(required_text, readme)
        self.assertIn("不是可用服务", readme)
        self.assertEqual(_find_positive_online_conclusions(readme), ())

        historical = _read_validation_evidence("historical_evidence.json")
        synthetic = _read_validation_evidence("synthetic_batch_evidence.json")
        live = _read_validation_evidence("live_smoke_evidence.json")
        replay = _read_validation_evidence("live_smoke_replay_evidence.json")

        historical_section = _markdown_section(readme, "1. 历史只读审计")
        translation_log = historical["logs"]["translation_log"]
        translation_line = _section_line(historical_section, "- translation log：")
        for field, unit in (
            ("total_lines", "个物理行"),
            ("successful_lines", "个成功记录"),
            ("failed_lines", "个失败记录"),
            ("unique_inputs", "个唯一输入"),
            ("unparseable_lines", "个无法解析行"),
        ):
            self.assertIn(f"{translation_log[field]} {unit}", translation_line)

        write_log = historical["logs"]["write_log"]
        write_line = _section_line(historical_section, "- write log：")
        for field, unit in (
            ("total_lines", "个物理行"),
            ("successful_lines", "个成功记录"),
            ("unique_inputs", "个唯一输入"),
            ("duplicates", "个重复记录"),
            ("unparseable_lines", "个无法解析行"),
        ):
            self.assertIn(f"{write_log[field]} {unit}", write_line)

        reconciliation = historical["workbook_reconciliation"]
        workbook_line = _section_line(historical_section, "- 工作簿对账：")
        for field, label in (
            ("verifiable_write_records", "个可核验写入记录"),
            ("successful_matches", "位置和值均匹配"),
            ("mismatches", "不一致"),
            ("unlocatable_records", "无法定位"),
        ):
            expected = (
                f"{reconciliation[field]} {label}"
                if field == "verifiable_write_records"
                else f"{label} {reconciliation[field]}"
            )
            self.assertIn(expected, workbook_line)

        synthetic_section = _markdown_section(readme, "2. 离线合成")
        dataset = synthetic["dataset"]
        for field, unit in (
            ("rows", "行"),
            ("input_cells", "个输入单元格"),
            ("unique_texts", "个唯一任务"),
            ("deduplicated_cells", "个去重命中"),
        ):
            self.assertIn(f"{dataset[field]} {unit}", synthetic_section)
        mock_usage = synthetic["mock_usage"]
        retry_count = synthetic["fault_injection"]["temporary_failure"][
            "retries_observed"
        ]
        second_run = synthetic["second_identical_run"]
        self.assertIn(
            f"mock 业务操作为 {mock_usage['batch_task_executions']} 次",
            synthetic_section,
        )
        self.assertIn(f"包含 {retry_count} 次受控重试", synthetic_section)
        self.assertIn(
            f"相同输入第二轮为 {second_run['batch_task_requests']} 次业务操作",
            synthetic_section,
        )
        self.assertIn(
            f"不是 {dataset['rows']} 次真实接口调用",
            synthetic_section,
        )
        self.assertIn(
            f"也不是 {mock_usage['batch_task_executions']} 次真实接口调用",
            synthetic_section,
        )

        live_section = _markdown_section(readme, "3. 当前真实")
        direction_counts = {
            direction["actual"] for direction in live["directions"].values()
        }
        self.assertEqual(len(direction_counts), 1)
        self.assertIn(f"两个方向各 {direction_counts.pop()} 条", live_section)
        self.assertIn(f"共 {live['actual_samples']} 条", live_section)
        self.assertIn(f"{live['successes']} 成功", live_section)
        self.assertIn(
            f"{live['error_counts']['empty_result']} `empty_result`",
            live_section,
        )
        self.assertIn(f"成功率 {live['success_rate']:.0%}", live_section)
        self.assertIn(f"状态为 `{live['status']}`", live_section)

        recovery_line = _section_line(live_section, "- 恢复轮：")
        for value, unit in (
            (live["request_count"], "个请求"),
            (live["cache_hits"], "个缓存命中"),
            (live["retries"], "次重试"),
        ):
            self.assertIn(f"{value} {unit}", recovery_line)
        self.assertFalse(live["proxy"]["enabled"])
        self.assertIn("代理关闭", recovery_line)

        replay_line = _section_line(live_section, "- 同输入复跑：")
        for value, unit in (
            (replay["cache_hits"], "个缓存命中"),
            (replay["request_count"], "个请求"),
            (replay["request_attempts"], "个尝试"),
            (replay["retries"], "次重试"),
        ):
            self.assertIn(f"{value} {unit}", replay_line)
        self.assertFalse(replay["proxy"]["enabled"])
        self.assertIn("代理关闭", replay_line)

    def test_root_readme_reports_current_limits_and_separates_evidence(self) -> None:
        """README 缺失、状态过期或混淆证据类型时，公开口径校验必须失败。"""
        self.assertTrue(ROOT_README.is_file(), "根目录 README.md 缺失")
        self._assert_root_readme_contract(
            ROOT_README.read_text(encoding="utf-8")
        )

    def test_root_readme_contract_rejects_semantic_and_evidence_mutations(self) -> None:
        """否定变肯定或任一证据章节数字漂移时，README 契约必须失败。"""
        readme = ROOT_README.read_text(encoding="utf-8")
        mutations = {
            "online conclusion becomes positive": readme.replace(
                "不是可用服务", "是可用服务", 1
            ),
            "historical count drifts": readme.replace(
                "translation log：1080 个物理行",
                "translation log：1081 个物理行",
                1,
            ),
            "synthetic count drifts": readme.replace(
                "验证：1000 行、2000 个输入单元格",
                "验证：999 行、2000 个输入单元格",
                1,
            ),
            "live count drifts": readme.replace(
                "当前聚合结果是 3 成功",
                "当前聚合结果是 4 成功",
                1,
            ),
            "live replay count drifts": readme.replace(
                "同输入复跑：20 个缓存命中",
                "同输入复跑：19 个缓存命中",
                1,
            ),
        }

        for label, mutated_readme in mutations.items():
            with self.subTest(mutation=label):
                self.assertNotEqual(mutated_readme, readme, "变异探针未命中原文")
                with self.assertRaises(AssertionError):
                    self._assert_root_readme_contract(mutated_readme)

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
