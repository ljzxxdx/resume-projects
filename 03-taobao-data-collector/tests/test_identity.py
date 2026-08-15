"""稳定 URL 与业务记录唯一键测试。"""

from __future__ import annotations

import importlib
import unittest

from taobao_collector.models import (
    CommentRecord,
    Engine,
    LiveRoomRecord,
    ProductRecord,
)


class IdentityTests(unittest.TestCase):
    def identity_module(self):
        try:
            return importlib.import_module("taobao_collector.identity")
        except ModuleNotFoundError:
            self.fail("identity 模块尚未实现")

    def test_url_normalization_removes_tracking_and_orders_query(self) -> None:
        identity = self.identity_module()

        normalized = identity.normalize_url(
            "HTTPS://ITEM.TAOBAO.COM:443/item.htm?"
            "spm=a1z10&id=123&utm_source=test&sku=blue#detail"
        )

        self.assertEqual(
            normalized,
            "https://item.taobao.com/item.htm?id=123&sku=blue",
        )

    def test_url_normalization_handles_relative_urls_and_credentials(
        self,
    ) -> None:
        identity = self.identity_module()

        self.assertEqual(
            identity.normalize_url(
                "//ITEM.TAOBAO.COM/item.htm?id=1&spm=x#detail"
            ),
            "https://item.taobao.com/item.htm?id=1",
        )
        self.assertEqual(
            identity.normalize_url(
                "https://user:secret@example.com:8443/item?id=1"
            ),
            "https://example.com:8443/item?id=1",
        )
        with self.assertRaisesRegex(ValueError, "来源 URL 格式无效"):
            identity.normalize_url(
                "https://user:secret@example.com:bad/item?id=1"
            )
        with self.assertRaisesRegex(ValueError, "来源 URL 格式无效"):
            identity.normalize_url("https://[::1")

    def test_product_key_prefers_platform_id_and_falls_back_to_url(self) -> None:
        identity = self.identity_module()
        with_id = ProductRecord(
            run_id="run-a",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id=" 1001 ",
            source_url="https://item.taobao.com/item.htm?id=1001&spm=a",
            name="茶杯",
        )
        same_id = ProductRecord(
            run_id="run-b",
            engine=Engine.SELENIUM,
            keyword="杯子",
            product_id="1001",
            source_url="https://detail.tmall.com/item.htm?id=1001",
            name="茶杯新标题",
        )
        without_id = ProductRecord(
            run_id="run-c",
            engine=Engine.SELENIUM,
            keyword="陶瓷",
            product_id="",
            source_url="HTTPS://ITEM.TAOBAO.COM/item.htm?spm=x&id=2002",
            name="瓷盘",
        )

        self.assertEqual(
            identity.product_unique_key(with_id),
            identity.product_unique_key(same_id),
        )
        self.assertEqual(
            identity.product_unique_key(with_id),
            "product:id:1001",
        )
        self.assertEqual(
            identity.product_unique_key(without_id),
            "product:url:https://item.taobao.com/item.htm?id=2002",
        )

    def test_comment_key_uses_product_user_and_content_digests(self) -> None:
        identity = self.identity_module()
        base = CommentRecord(
            run_id="run-a",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="1001",
            source_url="https://item.taobao.com/item.htm?id=1001",
            user_name=" 用户 甲 ",
            sku_info="白色",
            content="做工细致\n包装完整",
        )
        same = CommentRecord(
            run_id="run-b",
            engine=Engine.SELENIUM,
            keyword="杯子",
            product_id="1001",
            source_url="https://item.taobao.com/item.htm?id=1001&spm=x",
            user_name="用户 甲",
            sku_info="蓝色",
            content="做工细致 包装完整",
        )
        changed = CommentRecord(
            run_id="run-c",
            engine=Engine.SELENIUM,
            keyword="杯子",
            product_id="1001",
            source_url="https://item.taobao.com/item.htm?id=1001",
            user_name="用户乙",
            sku_info="白色",
            content="做工细致 包装完整",
        )

        key = identity.comment_unique_key(base)
        self.assertEqual(key, identity.comment_unique_key(same))
        self.assertNotEqual(key, identity.comment_unique_key(changed))
        self.assertRegex(
            key,
            r"^comment:product:id:1001:[0-9a-f]{64}:[0-9a-f]{64}$",
        )

    def test_comment_key_falls_back_to_normalized_product_url(self) -> None:
        identity = self.identity_module()
        first = CommentRecord(
            run_id="run-a",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="",
            source_url="//item.taobao.com/item.htm?id=8&spm=x",
            user_name="用户甲",
            sku_info="白色",
            content="做工细致",
        )
        second = CommentRecord(
            run_id="run-b",
            engine=Engine.SELENIUM,
            keyword="杯子",
            product_id="",
            source_url="https://ITEM.TAOBAO.COM/item.htm?id=8#detail",
            user_name="用户甲",
            sku_info="蓝色",
            content="做工细致",
        )

        self.assertEqual(
            identity.comment_unique_key(first),
            identity.comment_unique_key(second),
        )

    def test_missing_business_identity_is_rejected(self) -> None:
        identity = self.identity_module()
        product = ProductRecord(
            run_id="run-a",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="",
            source_url="",
            name="未知商品",
        )
        comment = CommentRecord(
            run_id="run-a",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            product_id="",
            source_url="",
            user_name="用户甲",
            sku_info="",
            content="内容",
        )
        live_room = LiveRoomRecord(
            run_id="run-a",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            live_room_id="",
            source_url="",
            account_name="",
            introduction="",
        )

        for record in (product, comment, live_room):
            with self.subTest(record=type(record).__name__):
                with self.assertRaises(ValueError):
                    identity.record_unique_key(record)

    def test_live_key_prefers_room_id_then_normalized_account(self) -> None:
        identity = self.identity_module()
        with_id = LiveRoomRecord(
            run_id="run-a",
            engine=Engine.DRISSION,
            keyword="陶瓷",
            live_room_id=" room-9 ",
            source_url="https://live.taobao.com/room?id=room-9",
            account_name="陶瓷馆",
            introduction="直播",
        )
        without_id = LiveRoomRecord(
            run_id="run-b",
            engine=Engine.SELENIUM,
            keyword="陶瓷",
            live_room_id="",
            source_url="",
            account_name=" 陶瓷　馆 ",
            introduction="直播",
        )
        same_account = LiveRoomRecord(
            run_id="run-c",
            engine=Engine.DRISSION,
            keyword="杯子",
            live_room_id="",
            source_url="",
            account_name="陶瓷 馆",
            introduction="另一场直播",
        )

        self.assertEqual(
            identity.live_room_unique_key(with_id),
            "live:id:room-9",
        )
        self.assertEqual(
            identity.live_room_unique_key(without_id),
            identity.live_room_unique_key(same_account),
        )
        self.assertRegex(
            identity.live_room_unique_key(without_id),
            r"^live:account:[0-9a-f]{64}$",
        )


if __name__ == "__main__":
    unittest.main()
