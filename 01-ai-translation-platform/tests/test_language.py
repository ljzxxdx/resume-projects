from __future__ import annotations

import unittest


class LanguageResolutionTests(unittest.TestCase):
    def resolve(self, text, from_lang=None, to_lang=None):
        from translation_platform.language import resolve_language_pair

        return resolve_language_pair(
            text=text,
            from_lang=from_lang,
            to_lang=to_lang,
        )

    def test_explicit_source_language_wins_over_text_detection(self) -> None:
        decision = self.resolve(
            text="这是合成中文文本",
            from_lang="en",
            to_lang="zh-CHS",
        )

        self.assertEqual(decision.from_lang, "en")
        self.assertEqual(decision.to_lang, "zh-CHS")
        self.assertFalse(decision.inferred_source)

    def test_explicit_source_can_translate_numbers_and_punctuation(self) -> None:
        decision = self.resolve(text="12345?!", from_lang="en")

        self.assertEqual((decision.from_lang, decision.to_lang), ("en", "zh-CHS"))
        self.assertFalse(decision.inferred_source)

    def test_default_detection_infers_chinese_and_english(self) -> None:
        chinese = self.resolve("合成文本 123")
        extended_chinese = self.resolve("\U00030000")
        english = self.resolve("Synthetic text 123")

        self.assertEqual((chinese.from_lang, chinese.to_lang), ("zh-CHS", "en"))
        self.assertEqual(extended_chinese.from_lang, "zh-CHS")
        self.assertEqual((english.from_lang, english.to_lang), ("en", "zh-CHS"))
        self.assertTrue(chinese.inferred_source)
        self.assertTrue(english.inferred_source)

    def test_mixed_text_uses_majority_then_first_signal_as_tie_breaker(self) -> None:
        chinese_majority = self.resolve("中文多 A")
        english_majority = self.resolve("English 文")
        english_first_tie = self.resolve("A中")
        chinese_first_tie = self.resolve("中A")

        self.assertEqual(chinese_majority.from_lang, "zh-CHS")
        self.assertEqual(english_majority.from_lang, "en")
        self.assertEqual(english_first_tie.from_lang, "en")
        self.assertEqual(chinese_first_tie.from_lang, "zh-CHS")

    def test_empty_and_signal_free_text_have_distinct_errors(self) -> None:
        from translation_platform.language import LanguageResolutionError

        with self.assertRaisesRegex(LanguageResolutionError, "text must not be empty"):
            self.resolve("   ")
        with self.assertRaisesRegex(LanguageResolutionError, "no supported language"):
            self.resolve("12345?!")

    def test_unsupported_script_and_language_parameters_are_rejected(self) -> None:
        from translation_platform.language import LanguageResolutionError

        with self.assertRaisesRegex(LanguageResolutionError, "unsupported script"):
            self.resolve("синтетический текст")
        with self.assertRaisesRegex(LanguageResolutionError, "unsupported language"):
            self.resolve("synthetic", from_lang="fr")
        with self.assertRaisesRegex(LanguageResolutionError, "must differ"):
            self.resolve("synthetic", from_lang="en", to_lang="en")

    def test_explicit_target_is_respected_after_source_inference(self) -> None:
        from translation_platform.language import LanguageResolutionError

        decision = self.resolve("synthetic text", to_lang="zh-CHS")
        self.assertEqual((decision.from_lang, decision.to_lang), ("en", "zh-CHS"))

        with self.assertRaisesRegex(LanguageResolutionError, "must differ"):
            self.resolve("synthetic text", to_lang="en")


if __name__ == "__main__":
    unittest.main()
