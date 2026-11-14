import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tg_summary_bot.semantic_search import semantic_search, select_sources, expanded_queries
from tg_summary_bot.storage import StoredMessage


def message(mid, text, sender=7):
    return StoredMessage(mid, 1, 'group', sender, 'Alice', text, '2026-11-05', None, 'incoming', 'text')


def store_for(messages, context=None):
    return SimpleNamespace(get_search_candidates=AsyncMock(return_value=messages),
                           get_search_context=AsyncMock(return_value=context or []))


class SemanticSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_expansion_then_contextual_selection(self):
        source = message(1, 'RTX 5090 наконец приехала, вечером ставлю в системник')
        store = store_for([source], [message(2, 'Поздравляю с обновлением!', sender=8)])
        lock = asyncio.Lock()

        async def complete(**kwargs):
            self.assertTrue(lock.locked())
            data = json.loads(kwargs['user'])
            self.assertIn('покупку видеокарты', data['query'])
            if 'messages' not in data:
                return '{"queries":["RTX GeForce покупка", "обновление компьютера"]}'
            self.assertEqual(data['selectable_ids'], [1])
            self.assertEqual(data['messages'][0]['context'][0]['message_id'], 2)
            return json.dumps({'matches': [{'message_id': 1, 'quote': source.text}]})

        client = SimpleNamespace(complete=AsyncMock(side_effect=complete), unload=AsyncMock())
        result = await semantic_search(store, client, lock, chat_id=1, before_id=3,
                                       query='где я рассказывал про покупку видеокарты', sender_id=7)
        self.assertEqual([m.message_id for m in result.messages], [1])
        self.assertIn('RTX GeForce покупка', store.get_search_candidates.await_args.kwargs['queries'])
        self.assertFalse(result.incomplete)
        self.assertEqual(client.unload.await_count, 2)
        self.assertFalse(lock.locked())

    async def test_forged_id_quote_or_author_is_rejected(self):
        for mid, quote, sender in ((99, 'text', None), (1, 'invented', None), (1, 'text', 8)):
            with self.assertRaises(RuntimeError):
                select_sources(json.dumps({'matches': [{'message_id': mid, 'quote': quote}]}), [message(1, 'text')], sender)

    async def test_bad_expansion_falls_back_to_original_query(self):
        client = SimpleNamespace(complete=AsyncMock(return_value='broken'), unload=AsyncMock())
        store = store_for([])
        with self.assertLogs(level='WARNING'):
            result = await semantic_search(store, client, asyncio.Lock(), chat_id=1, before_id=3, query='test')
        self.assertEqual(store.get_search_candidates.await_args.kwargs['queries'], ['test'])
        self.assertTrue(result.incomplete)
        for value in ('null', '{"queries":"query"}', '{"queries":[5]}', '{"queries":[]}'):
            with self.assertRaises(RuntimeError):
                expanded_queries(value)

    async def test_timeout_during_ranking_keeps_at_most_five_results(self):
        # Force separate batches and then interrupt the tournament.
        sources = [message(mid, f'candidate {mid} ' + 'x' * 1700) for mid in range(12)]
        store = store_for(sources)

        async def complete(**kwargs):
            data = json.loads(kwargs['user'])
            if 'messages' not in data:
                return '{"queries":["candidate"]}'
            if data['final']:
                await asyncio.sleep(10)
            return json.dumps({'matches': [{'message_id': m['message_id'], 'quote': m['text'][:20]} for m in data['messages'][:5]]})

        lock = asyncio.Lock()
        client = SimpleNamespace(complete=complete, unload=AsyncMock())
        result = await semantic_search(store, client, lock, chat_id=1, before_id=20, query='candidate', timeout=.05)
        self.assertTrue(result.incomplete)
        self.assertLessEqual(len(result.messages), 5)
        self.assertFalse(lock.locked())

    async def test_long_source_keeps_relevant_late_window(self):
        source = message(1, 'a' * 15000 + 'shader TARGET')
        store = store_for([source])

        async def complete(**kwargs):
            data = json.loads(kwargs['user'])
            if 'messages' not in data:
                return '{"queries":["shader"]}'
            text = data['messages'][0]['text']
            self.assertLessEqual(len(text), 3000)
            self.assertIn('TARGET', text)
            return '{"matches":[{"message_id":1,"quote":"shader TARGET"}]}'

        result = await semantic_search(store, SimpleNamespace(complete=complete, unload=AsyncMock()),
            asyncio.Lock(), chat_id=1, before_id=2, query='shader')
        self.assertEqual(result.messages[0].message_id, 1)

    async def test_external_cancellation_propagates_and_unloads(self):
        started = asyncio.Event()

        async def complete(**kwargs):
            started.set()
            await asyncio.sleep(10)

        lock = asyncio.Lock()
        client = SimpleNamespace(complete=complete, unload=AsyncMock())
        task = asyncio.create_task(semantic_search(store_for([]), client, lock, chat_id=1, before_id=2, query='meaning'))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(lock.locked())
        client.unload.assert_awaited_once()
