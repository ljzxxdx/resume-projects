import unittest

from scrapyWithRedis import items as items_module


class EmojiItemTests(unittest.TestCase):
    def test_declares_unified_fields(self):
        expected_fields = {
            "img_url",
            "title",
            "source_url",
            "image_path",
            "image_checksum",
            "download_status",
            "download_error",
            "crawled",
            "spider",
            "worker_id",
        }

        self.assertTrue(hasattr(items_module, "EmojiItem"))
        self.assertEqual(set(items_module.EmojiItem.fields), expected_fields)


if __name__ == "__main__":
    unittest.main()
