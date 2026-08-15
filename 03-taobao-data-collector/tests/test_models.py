"""Tests for normalized collection records and run summaries."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal

from taobao_collector import models


class RecordModelTests(unittest.TestCase):
    def assert_utc_datetime(self, value: datetime) -> None:
        self.assertIsNotNone(value.tzinfo)
        self.assertEqual(value.utcoffset(), timezone.utc.utcoffset(value))

    def test_enums_use_stable_export_values(self) -> None:
        self.assertEqual(models.Engine.DRISSION.value, "drission")
        self.assertEqual(models.Engine.SELENIUM.value, "selenium")
        self.assertEqual(models.RunMode.PRODUCTS.value, "products")
        self.assertEqual(models.RunMode.LIVE.value, "live")
        self.assertEqual(models.RecordStatus.RUNNING.value, "running")
        self.assertEqual(models.RecordStatus.SUCCESS.value, "success")
        self.assertEqual(models.RecordStatus.PARTIAL.value, "partial")
        self.assertEqual(models.RecordStatus.FAILED.value, "failed")
        self.assertEqual(models.RecordStatus.BLOCKED.value, "blocked")

    def test_product_record_keeps_normalized_business_fields(self) -> None:
        record = models.ProductRecord(
            run_id="run-001",
            engine=models.Engine.DRISSION,
            keyword="德化瓷",
            product_id="10001",
            source_url="https://item.taobao.com/item.htm?id=10001",
            name="青花瓷杯",
            price=Decimal("39.90"),
            sales_count=128,
        )

        self.assertEqual(record.run_id, "run-001")
        self.assertEqual(record.engine, models.Engine.DRISSION)
        self.assertEqual(record.keyword, "德化瓷")
        self.assertEqual(record.product_id, "10001")
        self.assertEqual(
            record.source_url,
            "https://item.taobao.com/item.htm?id=10001",
        )
        self.assertEqual(record.name, "青花瓷杯")
        self.assertEqual(record.price, Decimal("39.90"))
        self.assertEqual(record.sales_count, 128)
        self.assertEqual(record.status, models.RecordStatus.SUCCESS)
        self.assertIsNone(record.error_message)
        self.assert_utc_datetime(record.collected_at)

    def test_comment_record_is_linked_to_its_product(self) -> None:
        record = models.CommentRecord(
            run_id="run-002",
            engine=models.Engine.SELENIUM,
            keyword="苗族银饰",
            product_id="20002",
            source_url="https://item.taobao.com/item.htm?id=20002",
            user_name="用***户",
            sku_info="银色款",
            content="做工细致",
        )

        self.assertEqual(record.product_id, "20002")
        self.assertEqual(record.user_name, "用***户")
        self.assertEqual(record.sku_info, "银色款")
        self.assertEqual(record.content, "做工细致")
        self.assertEqual(record.status, models.RecordStatus.SUCCESS)
        self.assert_utc_datetime(record.collected_at)

    def test_live_room_record_keeps_normalized_counts(self) -> None:
        record = models.LiveRoomRecord(
            run_id="run-003",
            engine=models.Engine.SELENIUM,
            keyword="德化瓷",
            live_room_id="live-30003",
            source_url="https://live.taobao.com/room/30003",
            account_name="德化陶瓷馆",
            introduction="手工陶瓷直播",
            viewer_count=3200,
            follower_count=86000,
            product_count=25,
        )

        self.assertEqual(record.live_room_id, "live-30003")
        self.assertEqual(record.account_name, "德化陶瓷馆")
        self.assertEqual(record.introduction, "手工陶瓷直播")
        self.assertEqual(record.viewer_count, 3200)
        self.assertEqual(record.follower_count, 86000)
        self.assertEqual(record.product_count, 25)
        self.assertEqual(record.status, models.RecordStatus.SUCCESS)
        self.assert_utc_datetime(record.collected_at)

    def test_missing_numeric_values_are_represented_by_none(self) -> None:
        product = models.ProductRecord(
            run_id="run-004",
            engine=models.Engine.DRISSION,
            keyword="瓷器",
            product_id="40004",
            source_url="https://item.taobao.com/item.htm?id=40004",
            name="陶瓷摆件",
        )
        live_room = models.LiveRoomRecord(
            run_id="run-004",
            engine=models.Engine.DRISSION,
            keyword="瓷器",
            live_room_id="live-40004",
            source_url="https://live.taobao.com/room/40004",
            account_name="陶瓷直播间",
            introduction="",
        )

        self.assertIsNone(product.price)
        self.assertIsNone(product.sales_count)
        self.assertIsNone(live_room.viewer_count)
        self.assertIsNone(live_room.follower_count)
        self.assertIsNone(live_room.product_count)

    def test_run_summary_starts_with_zero_counts(self) -> None:
        summary = models.RunSummary(
            run_id="run-005",
            engine=models.Engine.SELENIUM,
            keyword="苗族银饰",
            mode=models.RunMode.LIVE,
        )

        self.assertEqual(summary.status, models.RecordStatus.RUNNING)
        self.assertIsNone(summary.ended_at)
        self.assertEqual(summary.product_count, 0)
        self.assertEqual(summary.comment_count, 0)
        self.assertEqual(summary.live_room_count, 0)
        self.assertEqual(summary.failed_count, 0)
        self.assertEqual(summary.success_count, 0)
        self.assertEqual(summary.duplicate_count, 0)
        self.assertIsNone(summary.error_message)
        self.assert_utc_datetime(summary.started_at)

    def test_records_are_immutable(self) -> None:
        product = models.ProductRecord(
            run_id="run-006",
            engine=models.Engine.DRISSION,
            keyword="瓷器",
            product_id="60006",
            source_url="https://item.taobao.com/item.htm?id=60006",
            name="瓷杯",
        )

        with self.assertRaises(FrozenInstanceError):
            product.status = models.RecordStatus.FAILED


if __name__ == "__main__":
    unittest.main()
