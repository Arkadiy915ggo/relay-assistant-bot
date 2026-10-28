import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tg_summary_bot.joke_awards import (
    QUALIFICATION_SYSTEM_PROMPT, JokeSelector, parse_joke_qualification,
)
from tg_summary_bot.storage import StoredMessage


def candidate(text):
    return StoredMessage(7, 1, "group", 10, "Author", text, datetime.now(timezone.utc).isoformat(),
                         None, "incoming", "caption")


def judgment(**changes):
    values = dict(has_joke=True, source_message_id=7, humor_type="absurdity",
                  evidence_quote="Колобок повесился.", needs_context=False,
                  reason="У круглого героя нет шеи: невозможная развязка.")
    values.update(changes)
    return json.dumps(values, ensure_ascii=False)


class JokeQualificationTests(unittest.IsolatedAsyncioTestCase):
    async def run_selector(self, text, response):
        client = SimpleNamespace(complete=AsyncMock(side_effect=[
            '{"has_joke":true,"source_message_id":7,"reason":"winner"}', response,
        ]))
        result = await JokeSelector(client).choose_messages([candidate(text)])
        return result, client

    async def test_plain_insult_is_rejected_even_after_primary_selector_picks_it(self):
        result, client = await self.run_selector("Вот гнида", judgment(
            has_joke=False, source_message_id=None, humor_type="none", evidence_quote="",
            reason="Обычная реакция без видимого комического механизма.",
        ))
        self.assertEqual(result.status, "none")
        self.assertIsNone(result.winner)
        self.assertEqual(client.complete.await_count, 2)
        self.assertEqual(client.complete.call_args.kwargs["system"], QUALIFICATION_SYSTEM_PROMPT)

    async def test_invented_image_or_chat_context_cannot_qualify_a_caption(self):
        result, _ = await self.run_selector("Вот гнида", judgment(
            humor_type="irony", evidence_quote="Вот гнида", needs_context=True,
            reason="Смешно в контексте картинки.",
        ))
        self.assertEqual(result.status, "none")
        contradictory, _ = await self.run_selector("Вот гнида", judgment(
            humor_type="irony", evidence_quote="Вот гнида", needs_context=False,
            reason="В контексте чата это неожиданно и смешно.",
        ))
        self.assertEqual(contradictory.status, "none")

    async def test_nonexistent_or_hidden_evidence_is_rejected(self):
        message = candidate("Пока он промямлит")
        for response in (judgment(), judgment(evidence_quote="Пока он промямлит", humor_type="reaction"),
                         judgment(evidence_quote="")):
            self.assertFalse(parse_joke_qualification(response, message, message.text).has_joke)
        long = candidate("visible " + "hidden joke " * 100 + "tail")
        self.assertFalse(parse_joke_qualification(judgment(evidence_quote="hidden joke"), long, "visible […] tail").has_joke)

    async def test_short_self_contained_joke_is_not_rejected_by_length(self):
        result, _ = await self.run_selector("Колобок повесился.", judgment())
        self.assertEqual(result.status, "selected")
        self.assertEqual(result.winner.message_id, 7)

    async def test_rough_joke_is_not_a_profanity_filter(self):
        text = "Жизнь — говно, зато удобрение для новых планов."
        result, _ = await self.run_selector(text, judgment(
            humor_type="comic_comparison", evidence_quote="удобрение для новых планов",
            reason="Негативная оценка превращается в буквальное удобрение.",
        ))
        self.assertEqual(result.status, "selected")

    async def test_broken_schema_wrong_id_and_truncated_json_never_qualify(self):
        message = candidate("Колобок повесился.")
        for response in ('{"has_joke":true', judgment(source_message_id=True), judgment(source_message_id=8),
                         judgment(needs_context="false"), judgment(extra=True)):
            self.assertFalse(parse_joke_qualification(response, message, message.text).valid)

    async def test_no_joke_selection_needs_no_extra_model_call(self):
        client = SimpleNamespace(complete=AsyncMock(return_value='{"has_joke":false,"source_message_id":null,"reason":"none"}'))
        self.assertEqual((await JokeSelector(client).choose_messages([candidate("Обычный текст")])).status, "none")
        client.complete.assert_awaited_once()
