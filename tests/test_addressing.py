import unittest
from types import SimpleNamespace

from tg_summary_bot.addressing import normalize_alias, remove_aliases_from_text, split_aliases
from tg_summary_bot.bot import extract_addressed_request


class AddressingTests(unittest.TestCase):
    def test_normalizes_case_yo_and_punctuation(self) -> None:
        self.assertEqual(normalize_alias(" Рёл-ейка! "), "рел ейка")

    def test_removes_alias_and_surrounding_punctuation(self) -> None:
        result = remove_aliases_from_text("Релейка, о чём договорились?", ["релейка"])
        self.assertEqual(result, "о чём договорились")

    def test_does_not_match_alias_inside_a_word(self) -> None:
        result = remove_aliases_from_text("ботаник написал сообщение", ["бот"])
        self.assertIsNone(result)

    def test_short_alias_requires_exact_match(self) -> None:
        result = remove_aliases_from_text("Релй, ответь", ["рел"])
        self.assertIsNone(result)

    def test_long_alias_allows_one_typo(self) -> None:
        result = remove_aliases_from_text("Релейк, ответь", ["релейка"])
        self.assertEqual(result, "ответь")

    def test_removes_multiple_aliases_once(self) -> None:
        result = remove_aliases_from_text("Реле, Релейка, ответь", ["реле", "релейка"])
        self.assertEqual(result, "ответь")

    def test_splits_unique_aliases(self) -> None:
        self.assertEqual(
            split_aliases("Реле, реле!, Релейка"),
            [("Реле", "реле"), ("Релейка", "релейка")],
        )

    def test_keeps_telegram_username_addressing(self) -> None:
        class Mention:
            type = "mention"

            def extract_from(self, text: str) -> str:
                return "@relay_bot"

        message = SimpleNamespace(
            text="@relay_bot расскажи новости",
            caption=None,
            entities=[Mention()],
            caption_entities=None,
        )
        self.assertEqual(
            extract_addressed_request(
                message,
                bot_id=1,
                bot_username="relay_bot",
                normalized_aliases=[],
            ),
            "расскажи новости",
        )

    def test_accepts_text_mention_for_bot_id(self) -> None:
        class Mention:
            type = "text_mention"
            user = SimpleNamespace(id=42)

            def extract_from(self, text: str) -> str:
                return "Реле"

        message = SimpleNamespace(
            text="Реле помоги",
            caption=None,
            entities=[Mention()],
            caption_entities=None,
        )
        self.assertEqual(
            extract_addressed_request(
                message,
                bot_id=42,
                bot_username=None,
                normalized_aliases=[],
            ),
            "помоги",
        )
