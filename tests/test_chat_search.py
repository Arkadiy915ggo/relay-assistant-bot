import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import SendMessage

from tg_summary_bot.bot import create_dispatcher, route_under_gpu_lock
from tg_summary_bot.chat_search import SELF_SEARCH_RE, is_chat_search_request, source_link
from tg_summary_bot.intent_router import IntentRouter
from tg_summary_bot.storage import MessageStore


NOW = datetime.now(timezone.utc)


class ChatSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_alias_search_requires_leading_explicit_keywords(self):
        for request in (
            'найди про видеокарту', 'поищи где я говорил про отпуск',
            'отыщи тот разговор', 'разыщи обсуждение', 'ищи про Blender',
            'выполни поиск про освещение', 'сделай поиск старого обсуждения',
            'проведи поиск', 'запусти поиск про проект', 'Пожалуйста, найди про отпуск',
        ):
            with self.subTest(request=request):
                self.assertTrue(is_chat_search_request(request))
                client = SimpleNamespace(complete=AsyncMock())
                self.assertEqual((await IntentRouter(client).route(request)).action, 'search')
                client.complete.assert_not_awaited()
        for request in (
            'покажи сообщение', 'помнишь где я говорил про отпуск?',
            'как найти сообщение?', 'надо найти сообщение', 'я сказал: найди сообщение',
            'не ищи сообщения', 'найти сообщение было сложно',
            'найди в википедии Ada Lovelace', 'поищи в интернете видеокарту',
        ):
            with self.subTest(request=request):
                self.assertFalse(is_chat_search_request(request))
                client = SimpleNamespace(complete=AsyncMock(return_value='{"action":"search","period":null,"query":null}'))
                route = await IntentRouter(client).route(request)
                self.assertFalse(route.valid)
                self.assertEqual(route.reason, 'search_not_explicit')

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MessageStore(Path(self.directory.name) / 'search.sqlite3')
        await self.store.init()

    async def save(self, message_id, *, chat_id=1, sender_id=7, text='Купил новую видеокарту', origin='incoming', kind='text'):
        await self.store.save_message(
            chat_id=chat_id, message_id=message_id, chat_type='supergroup', sender_id=sender_id,
            sender_name='Alice', text=text, created_at=NOW, reply_to_message_id=None, origin=origin, kind=kind, replace=True,
        )

    async def handler(self, *, allowed=None):
        async def complete(**kwargs):
            payload = json.loads(kwargs['user'])
            if 'messages' not in payload:
                return '{"queries":["видеокарта"]}'
            matches = [{'message_id': m['message_id'], 'quote': m['text']}
                       for m in payload['messages'] if m['message_id'] in payload['selectable_ids']
                       and 'видеокарту' in m['text']]
            return json.dumps({'matches': matches[:5]})
        dp = await create_dispatcher(
            SimpleNamespace(allowed_chat_ids=allowed or set()), self.store,
            SimpleNamespace(), SimpleNamespace(llm=SimpleNamespace(complete=complete, unload=AsyncMock())), None, SimpleNamespace(), SimpleNamespace(),
            SimpleNamespace(), SimpleNamespace(), None, None, asyncio.Lock(), SimpleNamespace(),
        )
        return next(h.callback for h in dp.message.handlers if h.callback.__name__ == 'search_command')

    def message(self, text):
        chat = SimpleNamespace(id=1, type='supergroup', username=None)
        response = SimpleNamespace(message_id=99, chat=chat)
        return SimpleNamespace(
            chat=chat, message_id=20, text=text, from_user=SimpleNamespace(id=7),
            answer=AsyncMock(return_value=response), reply=AsyncMock(return_value=response),
        )

    async def test_scope_author_provenance_and_request_exclusion(self):
        await self.save(1)
        await self.save(2, sender_id=8)
        await self.save(3, chat_id=2)
        await self.save(4, origin='assistant', kind='assistant_answer')
        await self.save(20)
        hits = await self.store.get_search_candidates(chat_id=1, before_id=20, queries=['видеокарта'], sender_id=7)
        self.assertEqual([hit.message_id for hit in hits], [1])
        self.assertEqual(await self.store.get_search_candidates(chat_id=1, before_id=20, queries=['видеокарта'], since=NOW + timedelta(days=1)), [])

    async def test_index_tracks_updates_and_fallback(self):
        await self.save(1, text='Ёжик купил видеокарту')
        await self.save(2, text='ЕЖИК купил видеокарту')
        hits = await self.store.get_search_candidates(chat_id=1, before_id=20, queries=['ежик'])
        self.assertEqual([h.message_id for h in hits], [2, 1])
        await self.save(2, text='нет совпадений')
        self.assertEqual([h.message_id for h in await self.store.get_search_candidates(chat_id=1, before_id=20, queries=['ежик'])], [1])
        self.store._message_search_fts_available = False
        self.assertEqual([h.message_id for h in await self.store.get_search_candidates(chat_id=1, before_id=20, queries=['ежик'])], [1])

    async def test_original_reply_and_my_messages(self):
        await self.save(1)
        await self.save(2, sender_id=8)
        message = self.message('/search мои сообщения про видеокарту')
        await (await self.handler())(message)
        kwargs = message.answer.await_args.kwargs
        self.assertEqual(kwargs['reply_parameters'].message_id, 1)
        self.assertFalse(kwargs['reply_parameters'].allow_sending_without_reply)
        self.assertIsNone(kwargs['parse_mode'])

    async def test_deleted_source_fallback_and_denied_access(self):
        await self.save(1)
        message = self.message('/search видеокарту')
        message.answer.side_effect = [TelegramBadRequest(
            method=SendMessage(chat_id=1, text='test'), message='Bad Request: message to be replied not found',
        ), SimpleNamespace(message_id=99, chat=message.chat)]
        await (await self.handler())(message)
        self.assertEqual(message.answer.await_count, 2)
        self.assertIn('Оригинал недоступен', message.answer.await_args.args[0])
        denied = self.message('/search видеокарту')
        await (await self.handler(allowed={2}))(denied)
        denied.answer.assert_not_awaited()
        denied.reply.assert_not_awaited()

    async def test_empty_no_results_and_period(self):
        handler = await self.handler()
        for query in ('/search', '/search 1h несуществующее'):
            message = self.message(query)
            await handler(message)
            self.assertGreaterEqual(message.reply.await_count, 1)

    async def test_explicit_natural_search_without_model_and_source_links(self):
        await self.save(1)
        client = SimpleNamespace(complete=AsyncMock())
        request = 'найди сообщение где я говорил что купил видеокарту'
        route = await IntentRouter(client).route(request)
        self.assertEqual(route.action, 'search')
        client.complete.assert_not_awaited()
        lock = asyncio.Lock()
        async with lock:
            self.assertEqual((await asyncio.wait_for(
                route_under_gpu_lock(SimpleNamespace(), lock, request), timeout=0.1,
            )).action, 'search')
        self.assertTrue(SELF_SEARCH_RE.search('где я говорил'))
        self.assertTrue(SELF_SEARCH_RE.search('мои сообщения'))
        self.assertEqual(source_link(-1001234, 'supergroup', 5), 'https://t.me/c/1234/5')
        self.assertEqual(source_link(-1001234, 'supergroup', 5, 'chat'), 'https://t.me/chat/5')
        self.assertIsNone(source_link(-1234, 'group', 5))
