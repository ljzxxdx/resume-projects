"""Tests for browser-independent collector contracts."""

from __future__ import annotations

import unittest

from taobao_collector.collectors import base


class ProductFilterPolicyTests(unittest.TestCase):
    def policy_class(self):
        policy_class = getattr(base, "ProductFilterPolicy", None)
        self.assertIsNotNone(policy_class, "ProductFilterPolicy 尚未实现")
        return policy_class

    def test_default_policy_preserves_baseline_thresholds(self) -> None:
        policy = self.policy_class()()

        self.assertTrue(policy.enabled)
        self.assertFalse(policy.accepts_sales(29))
        self.assertTrue(policy.accepts_sales(30))
        self.assertFalse(policy.accepts_comments(19))
        self.assertTrue(policy.accepts_comments(20))
        try:
            missing_sales_accepted = policy.accepts_sales(None)
            missing_comments_accepted = policy.accepts_comments(None)
        except TypeError as exc:
            self.fail(f"缺失数量应被筛选策略正常处理：{exc}")
        self.assertFalse(missing_sales_accepted)
        self.assertFalse(missing_comments_accepted)

    def test_disabled_policy_accepts_values_below_thresholds(self) -> None:
        policy = self.policy_class()(enabled=False)

        self.assertTrue(policy.accepts_sales(0))
        self.assertTrue(policy.accepts_comments(0))

    def test_negative_threshold_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.policy_class()(min_sales_count=-1)

        with self.assertRaises(ValueError):
            self.policy_class()(min_comment_count=-1)


class CollectionResultTests(unittest.TestCase):
    def test_empty_result_has_separate_immutable_record_groups(self) -> None:
        result_class = getattr(base, "CollectionResult", None)
        self.assertIsNotNone(result_class, "CollectionResult 尚未实现")
        result = result_class()

        self.assertEqual(result.products, ())
        self.assertEqual(result.comments, ())
        self.assertEqual(result.live_rooms, ())


class ReadOnlyBehaviorPolicyTests(unittest.TestCase):
    def policy_class(self):
        policy_class = getattr(base, "ReadOnlyBehaviorPolicy", None)
        self.assertIsNotNone(
            policy_class,
            "ReadOnlyBehaviorPolicy 尚未实现",
        )
        return policy_class

    def test_policy_has_conservative_bounded_defaults(self) -> None:
        policy = self.policy_class()()

        self.assertFalse(policy.enabled)
        self.assertEqual(policy.min_pause, 3.0)
        self.assertEqual(policy.max_pause, 6.0)
        self.assertEqual(policy.comment_min_pause, 3.0)
        self.assertEqual(policy.comment_max_pause, 5.0)
        self.assertEqual(policy.product_min_pause, 5.0)
        self.assertEqual(policy.product_max_pause, 8.0)
        self.assertEqual(policy.page_min_pause, 30.0)
        self.assertEqual(policy.page_max_pause, 60.0)
        self.assertEqual(policy.max_scrolls, 2)
        self.assertEqual(policy.max_tab_views, 2)

    def test_policy_rejects_invalid_bounds(self) -> None:
        invalid_arguments = (
            {"min_pause": -0.1},
            {"min_pause": float("nan")},
            {"max_pause": float("inf")},
            {"min_pause": 1.0, "max_pause": 0.5},
            {"comment_min_pause": 2.0, "comment_max_pause": 1.0},
            {"product_min_pause": 2.0, "product_max_pause": 1.0},
            {"page_min_pause": 2.0, "page_max_pause": 1.0},
            {"max_scrolls": -1},
            {"max_tab_views": -1},
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.policy_class()(**arguments)


class LiveBehaviorPolicyTests(unittest.TestCase):
    def policy_class(self):
        policy_class = getattr(base, "LiveBehaviorPolicy", None)
        self.assertIsNotNone(
            policy_class,
            "LiveBehaviorPolicy 尚未实现",
        )
        return policy_class

    def test_policy_has_conservative_bounded_defaults(self) -> None:
        policy = self.policy_class()()

        self.assertFalse(policy.enabled)
        self.assertEqual(policy.min_pause, 6.0)
        self.assertEqual(policy.max_pause, 10.0)
        self.assertEqual(policy.room_min_pause, 8.0)
        self.assertEqual(policy.room_max_pause, 12.0)
        self.assertEqual(policy.max_scrolls, 2)

    def test_policy_rejects_invalid_bounds(self) -> None:
        invalid_arguments = (
            {"min_pause": -0.1},
            {"min_pause": float("nan")},
            {"max_pause": float("inf")},
            {"min_pause": 3.0, "max_pause": 2.0},
            {"room_min_pause": 3.0, "room_max_pause": 2.0},
            {"max_scrolls": -1},
        )

        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.policy_class()(**arguments)


class RetryPolicyTests(unittest.TestCase):
    def policy_class(self):
        policy_class = getattr(base, "RetryPolicy", None)
        self.assertIsNotNone(policy_class, "RetryPolicy 尚未实现")
        return policy_class

    def test_delays_start_above_zero_and_match_retry_count(self) -> None:
        policy = self.policy_class()(
            max_retries=3,
            base_delay=2.0,
        )

        self.assertEqual(policy.delays, (2.0, 4.0, 6.0))
        self.assertEqual(policy.max_attempts, 4)

    def test_default_retry_uses_conservative_backoff(self) -> None:
        policy = self.policy_class()()

        self.assertEqual(policy.delays, (10.0, 20.0, 30.0))

    def test_retry_policy_rejects_invalid_values(self) -> None:
        for arguments in (
            {"max_retries": -1},
            {"base_delay": 0},
            {"base_delay": -1},
            {"base_delay": float("nan")},
            {"base_delay": float("inf")},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.policy_class()(**arguments)


class WaitPolicyTests(unittest.TestCase):
    def policy_class(self):
        policy_class = getattr(base, "WaitPolicy", None)
        self.assertIsNotNone(policy_class, "WaitPolicy 尚未实现")
        return policy_class

    def test_policy_has_finite_defaults(self) -> None:
        policy = self.policy_class()()

        self.assertEqual(policy.timeout, 20.0)
        self.assertEqual(policy.poll_interval, 0.05)

    def test_policy_rejects_non_positive_values(self) -> None:
        for arguments in (
            {"timeout": 0},
            {"timeout": -1},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
            {"poll_interval": 0},
            {"poll_interval": -1},
            {"poll_interval": float("nan")},
            {"poll_interval": float("inf")},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    self.policy_class()(**arguments)


if __name__ == "__main__":
    unittest.main()
