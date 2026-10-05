import unittest

from tg_summary_bot.assistant import ChatAssistant


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def complete(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return "Реле"

    async def unload(self) -> None:
        pass


class ChatAssistantIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_includes_chat_scoped_aliases_in_answer_context(self) -> None:
        llm = FakeLLM()
        assistant = ChatAssistant(llm, chunk_chars=1000)  # type: ignore[arg-type]

        await assistant.ask([], "24h", "Как тебя зовут?", bot_names=["Реле", "Релейка"])

        prompt = str(llm.calls[0]["user"])
        self.assertIn("«Реле», «Релейка»", prompt)
        self.assertIn("как тебя зовут", prompt)
