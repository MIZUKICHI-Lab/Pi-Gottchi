import os
import sys
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from intents import detect_control_intent, normalize_utterance  # noqa: E402


class IntentTests(unittest.TestCase):
    def test_sleep_phrases_are_conservative_but_natural(self):
        positives = (
            "おやすみ", "お休みなさい。", "もこちゃん、おやすみ！",
            "じゃあ、おやすみ", "もう寝るね", "今日はもう寝るよ",
            "もう寝よう", "寝て", "スリープしてね",
            "さようなら", "バイバイ", "じゃあね", "またね、また明日",
        )
        for phrase in positives:
            with self.subTest(phrase=phrase):
                self.assertEqual("sleep", detect_control_intent(phrase))

    def test_mentions_and_negations_do_not_trigger(self):
        negatives = (
            "おやすみってどういう意味？", "おやすみと言って",
            "まだ寝ない", "明日寝ようかな", "もこは寝てるの？",
            "さようならを英語で何て言う？", "バイバイしたくない",
        )
        for phrase in negatives:
            with self.subTest(phrase=phrase):
                self.assertIsNone(detect_control_intent(phrase))

    def test_normalization_keeps_katakana_long_vowel(self):
        self.assertEqual("スリープして", normalize_utterance(" スリープして！ "))


if __name__ == "__main__":
    unittest.main()
