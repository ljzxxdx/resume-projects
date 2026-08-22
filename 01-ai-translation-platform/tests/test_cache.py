"""缓存键规范化和批量去重的行为测试。"""

from __future__ import annotations

import unittest

from translation_platform.batch_input import CellPosition, SourceCell
from translation_platform.cache import BatchTask, build_cache_key, deduplicate_cells, normalize_cache_text


class CacheTextTests(unittest.TestCase):
    """验证等价文本共享稳定且不泄露原文的缓存键。"""

    def test_normalize_cache_text_unifies_nfkc_and_whitespace(self) -> None:
        """全角字符、首尾空白及连续空白规范为同一文本。"""

        self.assertEqual(normalize_cache_text("  ＡＢＣ\t\n  "), "ABC")

    def test_build_cache_key_uses_normalized_text_and_language_direction(self) -> None:
        """相同方向的等价文本共享摘要，反向语言得到不同摘要。"""

        forward_key = build_cache_key("zh", "en", "  ｓａｍｅ\t text  ")
        reverse_key = build_cache_key("en", "zh", "same text")

        self.assertEqual(
            forward_key,
            "e9a9232dcab8cbe3d1771c0a303ef30560cc106810f24aad1b2db9ae8d6349ff",
        )
        self.assertEqual(
            reverse_key,
            "0e633929ff4ae5bebc34f14a20f1ad4fe708b47dd93c08fff89556c147a1e672",
        )
        self.assertNotEqual(forward_key, reverse_key)
        self.assertNotIn("same text", forward_key)

    def test_build_cache_key_distinguishes_fields_containing_nul(self) -> None:
        """字段内包含 NUL 时，不同三元组仍必须产生不同缓存键。"""

        first_key = build_cache_key("a\0b", "c", "d")
        second_key = build_cache_key("a", "b", "c\0d")

        self.assertNotEqual(first_key, second_key)


class DeduplicateCellsTests(unittest.TestCase):
    """验证重复源单元格只形成一个任务且保留原始位置。"""

    def test_deduplicate_cells_groups_three_equivalent_cells_in_first_seen_order(self) -> None:
        """三个等价单元格合并为一个任务，并保留三个位置。"""

        first = SourceCell(CellPosition(2, 2, "标题"), " same text ")
        second = SourceCell(CellPosition(3, 2, "标题"), "same\ttext")
        third = SourceCell(CellPosition(4, 3, "说明"), "ｓａｍｅ text")

        tasks = deduplicate_cells((first, second, third), "zh", "en")

        self.assertEqual(
            tasks,
            (
                BatchTask(
                    cache_key="e9a9232dcab8cbe3d1771c0a303ef30560cc106810f24aad1b2db9ae8d6349ff",
                    source_lang="zh",
                    target_lang="en",
                    normalized_text="same text",
                    positions=(first.position, second.position, third.position),
                ),
            ),
        )

    def test_deduplicate_cells_keeps_distinct_tasks_in_first_seen_order(self) -> None:
        """不同文本任务按其第一次出现的顺序输出。"""

        later_duplicate = SourceCell(CellPosition(2, 2, "标题"), "same text")
        first_distinct = SourceCell(CellPosition(2, 3, "说明"), "ＡＢＣ")
        tasks = deduplicate_cells((later_duplicate, first_distinct), "zh", "en")

        self.assertEqual(
            tuple((task.cache_key, task.normalized_text) for task in tasks),
            (
                ("e9a9232dcab8cbe3d1771c0a303ef30560cc106810f24aad1b2db9ae8d6349ff", "same text"),
                ("53c0aad4b513ab37595f47b53596f95f6ccb3f60dce3e32f71835903ac5d60d7", "ABC"),
            ),
        )

    def test_deduplicate_cells_skips_text_normalized_to_empty(self) -> None:
        """直接去重时也不为纯空白单元格创建无意义任务。"""

        whitespace = SourceCell(CellPosition(2, 2, "标题"), " \t\n ")
        retained = SourceCell(CellPosition(3, 2, "标题"), "保留文本")

        tasks = deduplicate_cells((whitespace, retained), "zh", "en")

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].normalized_text, "保留文本")
        self.assertEqual(tasks[0].positions, (retained.position,))


if __name__ == "__main__":
    unittest.main()
