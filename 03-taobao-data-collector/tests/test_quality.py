"""不依赖网站的数据质量统计测试。"""

from __future__ import annotations

import unittest

from taobao_collector.models import (
    CommentRecord,
    Engine,
    ProductRecord,
    RecordStatus,
)
from taobao_collector.quality import build_data_quality


class DataQualityTests(unittest.TestCase):
    def test_reports_counts_missing_required_values_and_bad_statuses(
        self,
    ) -> None:
        product = ProductRecord(
            run_id="quality-run",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="1001",
            source_url="https://item.taobao.com/item.htm?id=1001",
            name="茶杯",
        )
        invalid_product = ProductRecord(
            run_id="quality-run",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="",
            source_url="",
            name="",
            status=RecordStatus.PARTIAL,
        )
        invalid_comment = CommentRecord(
            run_id="quality-run",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="1001",
            source_url="https://item.taobao.com/item.htm?id=1001",
            user_name="用户甲",
            sku_info="",
            content="",
            status=RecordStatus.FAILED,
        )
        table_stats = {
            "products": {
                "new_count": 1,
                "duplicate_count": 1,
                "missing_count": 1,
                "failed_count": 1,
            },
            "comments": {
                "new_count": 1,
                "duplicate_count": 0,
                "missing_count": 0,
                "failed_count": 1,
            },
        }

        quality = build_data_quality(
            {
                "products": (product, product, invalid_product),
                "comments": (invalid_comment,),
                "live_rooms": (),
            },
            table_stats,
        )

        self.assertEqual(
            quality["tables"]["products"],
            {
                "record_count": 3,
                "unique_count": 1,
                "duplicate_count": 1,
                "required_field_missing_count": 2,
                "required_field_missing_by_field": {
                    "business_identity": 1,
                    "name": 1,
                },
                "abnormal_status_count": 1,
            },
        )
        self.assertEqual(
            quality["tables"]["comments"]
            ["required_field_missing_by_field"],
            {"content": 1},
        )
        self.assertEqual(
            quality["tables"]["comments"]["abnormal_status_count"],
            1,
        )
        self.assertEqual(
            quality["tables"]["live_rooms"]["record_count"],
            0,
        )


if __name__ == "__main__":
    unittest.main()
