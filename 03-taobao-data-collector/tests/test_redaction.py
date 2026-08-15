"""公开样例脱敏策略测试。"""

from __future__ import annotations

import json
import unittest

from taobao_collector.identity import record_unique_key
from taobao_collector.models import (
    CommentRecord,
    Engine,
    LiveRoomRecord,
    ProductRecord,
)
from taobao_collector.redaction import (
    build_redacted_sample,
    mask_user_name,
)


class RedactionTests(unittest.TestCase):
    def test_masks_names_without_revealing_short_names(self) -> None:
        self.assertEqual(mask_user_name(""), "")
        self.assertEqual(mask_user_name("张"), "*")
        self.assertEqual(mask_user_name("张三"), "张***")
        self.assertEqual(mask_user_name("张三丰"), "张***")
        self.assertEqual(mask_user_name("A\u0301B"), "Á***")
        self.assertEqual(mask_user_name("张\u200d三"), "张***")

    def test_only_explicitly_approved_comments_are_in_limited_sample(
        self,
    ) -> None:
        approved = CommentRecord(
            run_id="sensitive-run-id",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="sensitive-product-id",
            source_url="https://example.test/sensitive-product-id",
            user_name="张三丰",
            sku_info="白色",
            content="人工确认可公开",
        )
        private = CommentRecord(
            run_id="sensitive-run-id",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="sensitive-product-id",
            source_url="https://example.test/sensitive-product-id",
            user_name="李四",
            sku_info="蓝色",
            content="未经确认不得公开",
        )
        sample = build_redacted_sample(
            {
                "products": tuple(
                    ProductRecord(
                        run_id="sensitive-run-id",
                        engine=Engine.DRISSION,
                        keyword="陶瓷",
                        product_id=f"private-product-{index}",
                        source_url=f"https://example.test/{index}",
                        name=f"商品{index}",
                    )
                    for index in range(5)
                ),
                "comments": (approved, private),
                "live_rooms": (
                    LiveRoomRecord(
                        run_id="sensitive-run-id",
                        engine=Engine.DRISSION,
                        keyword="陶瓷",
                        live_room_id="private-room-id",
                        source_url="https://live.example.test/private-room-id",
                        account_name="主播甲乙",
                        introduction="陶瓷专场",
                    ),
                ),
            },
            approved_comment_keys={record_unique_key(approved)},
            max_records_per_table=3,
        )
        serialized = json.dumps(sample, ensure_ascii=False)

        self.assertEqual(len(sample["products"]), 3)
        self.assertEqual(len(sample["comments"]), 1)
        self.assertEqual(
            sample["comments"][0]["user_name"],
            "张***",
        )
        self.assertEqual(
            sample["comments"][0]["content"],
            "人工确认可公开",
        )
        self.assertEqual(
            sample["live_rooms"][0]["account_name"],
            "主***",
        )
        for secret in (
            "未经确认不得公开",
            "sensitive-run-id",
            "sensitive-product-id",
            "private-room-id",
            "https://example.test",
            "https://live.example.test",
        ):
            self.assertNotIn(secret, serialized)

    def test_no_comment_content_is_public_by_default(self) -> None:
        comment = CommentRecord(
            run_id="run-default-private",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="1001",
            source_url="https://example.test/1001",
            user_name="用户甲",
            sku_info="",
            content="默认不能公开",
        )

        sample = build_redacted_sample({"comments": (comment,)})

        self.assertEqual(sample["comments"], [])

    def test_sample_defensively_deduplicates_all_tables(self) -> None:
        product = ProductRecord(
            run_id="run-duplicate",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="1001",
            source_url="https://example.test/1001",
            name="茶杯",
        )
        comment = CommentRecord(
            run_id="run-duplicate",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="1001",
            source_url="https://example.test/1001",
            user_name="用户甲",
            sku_info="",
            content="允许公开",
        )
        live_room = LiveRoomRecord(
            run_id="run-duplicate",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            live_room_id="room-1",
            source_url="https://live.example.test/room-1",
            account_name="主播甲",
            introduction="陶瓷专场",
        )

        sample = build_redacted_sample(
            {
                "products": (product, product),
                "comments": (comment, comment, comment),
                "live_rooms": (live_room, live_room),
            },
            approved_comment_keys={record_unique_key(comment)},
        )

        self.assertEqual(len(sample["products"]), 1)
        self.assertEqual(len(sample["comments"]), 1)
        self.assertEqual(len(sample["live_rooms"]), 1)


if __name__ == "__main__":
    unittest.main()
