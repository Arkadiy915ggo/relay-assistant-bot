import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from tg_summary_bot.storage import MessageStore


class SearchIndexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MessageStore(Path(self.directory.name) / 'index.sqlite3')
        await self.store.init()

    async def save(self, mid, text, *, chat=1, sender=7, reply=None):
        await self.store.save_message(chat_id=chat, message_id=mid, chat_type='supergroup',
            sender_id=sender, sender_name='Participant', text=text,
            created_at=datetime.now(timezone.utc), reply_to_message_id=reply, origin='incoming', kind='text')

    async def test_old_source_retrieved_without_reading_thousand_recent_messages(self):
        await self.save(1, 'План: шейдер воды, туман, облака, эффекты лучей, оптимизация отрисовки')
        # Bulk insertion exercises index triggers, not a special search-only write path.
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.executemany(
                """INSERT INTO messages(chat_id,message_id,chat_type,sender_id,sender_name,text,created_at,origin,kind)
                   VALUES(1,?,'group',7,'Participant',?,'2026-11-05','incoming','text')""",
                [(mid, f'Обычный разговор номер {mid}') for mid in range(2, 1202)],
            )
            await db.commit()
        hits = await self.store.get_search_candidates(chat_id=1, before_id=1300, sender_id=7,
            queries=['список заданий для улучшений графики', 'шейдеры эффекты отрисовка'])
        self.assertIn(1, [hit.message_id for hit in hits])
        self.assertLessEqual(len(hits), 30)

    async def test_backfill_idempotency_delete_and_no_fts_fallback(self):
        await self.save(1, 'Туман шейдер облаков')
        async with aiosqlite.connect(self.store.database_path) as db:
            for suffix in ('insert', 'update', 'delete'):
                await db.execute(f'DROP TRIGGER messages_search_{suffix}')
            await db.execute('DROP TABLE messages_search_fts')
            await db.commit()
        await self.store.init()
        await self.store.init()
        kwargs = dict(chat_id=1, before_id=10, queries=['облака'])
        self.assertEqual([m.message_id for m in await self.store.get_search_candidates(**kwargs)], [1])
        self.store._message_search_fts_available = False
        self.assertEqual([m.message_id for m in await self.store.get_search_candidates(**kwargs)], [1])
        async with aiosqlite.connect(self.store.database_path) as db:
            await db.execute('DELETE FROM messages WHERE chat_id=1')
            await db.commit()
        self.store._message_search_fts_available = True
        self.assertEqual(await self.store.get_search_candidates(**kwargs), [])

    async def test_context_is_chat_scoped_snapshot_bounded_and_reply_aware(self):
        await self.save(1, 'Обсуждаем графику', sender=8)
        await self.save(2, 'Другая тема')
        await self.save(3, 'Согласен с этим', reply=1)
        await self.save(4, 'Соседняя реплика')
        await self.save(5, 'Будущее сообщение')
        await self.save(2, 'Чужой чат', chat=2)
        context = await self.store.get_search_context(chat_id=1, message_id=3, before_id=5, reply_to=1)
        self.assertEqual({m.message_id for m in context}, {1, 2, 4})
        self.assertTrue(all(m.chat_id == 1 for m in context))

    async def test_user_text_cannot_inject_fts_or_sql(self):
        await self.save(1, 'private source')
        for query in ('" OR 1=1 --', 'NEAR(private, source)', '*', '\"; DROP TABLE messages; --'):
            hits = await self.store.get_search_candidates(chat_id=2, before_id=10, queries=[query])
            self.assertEqual(hits, [])
