import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tg_summary_bot.storage import MessageStore


class ChatBotAliasStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_aliases_are_unique_and_scoped_to_a_chat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = MessageStore(Path(directory) / "messages.sqlite3")
            await store.init()
            now = datetime.now(timezone.utc)

            self.assertTrue(
                await store.add_chat_bot_alias(
                    chat_id=1,
                    alias="Реле",
                    normalized_alias="реле",
                    created_by_user_id=10,
                    created_at=now,
                )
            )
            self.assertFalse(
                await store.add_chat_bot_alias(
                    chat_id=1,
                    alias="реле",
                    normalized_alias="реле",
                    created_by_user_id=10,
                    created_at=now,
                )
            )
            self.assertTrue(
                await store.add_chat_bot_alias(
                    chat_id=2,
                    alias="Реле",
                    normalized_alias="реле",
                    created_by_user_id=20,
                    created_at=now,
                )
            )

            self.assertEqual([item.alias for item in await store.get_chat_bot_aliases(1)], ["Реле"])
            self.assertEqual([item.alias for item in await store.get_chat_bot_aliases(2)], ["Реле"])
            self.assertTrue(await store.remove_chat_bot_alias(chat_id=1, normalized_alias="реле"))
            self.assertFalse(await store.get_chat_bot_aliases(1))
            self.assertEqual(len(await store.get_chat_bot_aliases(2)), 1)
