import asyncio
import unittest

import tg_summary_bot.intent_router as intent_router
from tg_summary_bot.intent_router import IntentRouter, is_joke_request, parse_route_response
from tg_summary_bot.bot import route_under_gpu_lock


class FakeLLM:
    def __init__(self, response: str | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.unloaded = False

    async def complete(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response or ""

    async def unload(self) -> None:
        self.unloaded = True


class IntentRouteParserTests(unittest.TestCase):
    def test_recognizes_joke_requests_without_calling_them_memes(self) -> None:
        self.assertTrue(is_joke_request("пошути, пожалуйста"))
        self.assertTrue(is_joke_request("расскажи шутку"))
        self.assertFalse(is_joke_request("сделай мем"))
    def test_accepts_valid_object_and_ignores_extra_keys(self) -> None:
        route = parse_route_response('{"action":"summary","period":"6h","query":null,"extra":1}')
        self.assertTrue(route.valid)
        self.assertEqual((route.action, route.period, route.query), ("summary", "6h", None))

    def test_rejects_all_non_object_or_non_json_forms(self) -> None:
        for value in (
            "```json\n{\"action\":\"summary\",\"period\":null,\"query\":null}\n```",
            'prefix {"action":"summary","period":null,"query":null}',
            '[{"action":"summary","period":null,"query":null}]',
            '"summary"',
            '{"action":"summary","period":null,"query":null}{"action":"none"}',
        ):
            with self.subTest(value=value):
                self.assertFalse(parse_route_response(value).valid)

    def test_rejects_unknown_action_and_wrong_types(self) -> None:
        for value in (
            '{"action":"unknown","period":null,"query":null}',
            '{"action":1,"period":null,"query":null}',
            '{"action":"summary","period":3,"query":null}',
            '{"action":"wiki","period":null,"query":[]}',
            '{"action":"wiki","period":null,"query":""}',
        ):
            with self.subTest(value=value):
                self.assertFalse(parse_route_response(value).valid)

    def test_rejects_invalid_or_excessive_period_and_query(self) -> None:
        self.assertFalse(parse_route_response('{"action":"summary","period":"366d","query":null}').valid)
        self.assertFalse(parse_route_response('{"action":"summary","period":"not-a-period","query":null}').valid)
        self.assertFalse(parse_route_response('{"action":"wiki","period":null,"query":"' + "a" * 501 + '"}').valid)
        self.assertFalse(parse_route_response('{"action":"summary","period":"' + "1" * 33 + 'h","query":null}').valid)


class IntentRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_truncated_input_as_json(self) -> None:
        llm = FakeLLM('{"action":"question","period":null,"query":"rewritten"}')
        router = IntentRouter(llm)  # type: ignore[arg-type]
        route = await router.route("x" * 2001)
        self.assertEqual(route.action, "question")
        self.assertEqual(len(llm.calls[0]["user"]), 2000)
        self.assertEqual(llm.calls[0]["response_format"], "json")

    async def test_model_error_becomes_fallback(self) -> None:
        router = IntentRouter(FakeLLM(error=RuntimeError("offline")))  # type: ignore[arg-type]
        route = await router.route("hello")
        self.assertFalse(route.valid)
        self.assertEqual(route.reason, "model_error")

    async def test_rejects_meme_route_without_an_explicit_meme_request(self) -> None:
        router = IntentRouter(FakeLLM('{"action":"meme","period":null,"query":null}'))  # type: ignore[arg-type]
        route = await router.route("просто расскажи шутку")
        self.assertFalse(route.valid)
        self.assertEqual(route.reason, "meme_not_explicit")

    async def test_accepts_explicit_meme_request(self) -> None:
        router = IntentRouter(FakeLLM('{"action":"meme","period":null,"query":null}'))  # type: ignore[arg-type]
        route = await router.route("сделай мем из этой картинки")
        self.assertTrue(route.valid)
        self.assertEqual(route.action, "meme")

    async def test_timeout_becomes_fallback(self) -> None:
        class SlowLLM(FakeLLM):
            async def complete(self, **kwargs: object) -> str:
                await asyncio.sleep(1)
                return "{}"

        previous_timeout = intent_router.ROUTER_TIMEOUT_SECONDS
        intent_router.ROUTER_TIMEOUT_SECONDS = 0.01
        try:
            router = IntentRouter(SlowLLM())  # type: ignore[arg-type]
            route = await router.route("hello")
        finally:
            intent_router.ROUTER_TIMEOUT_SECONDS = previous_timeout
        self.assertFalse(route.valid)
        self.assertEqual(route.reason, "timeout")

    async def test_unload_finishes_before_unlock_and_action(self) -> None:
        events: list[str] = []
        lock = asyncio.Lock()
        case = self

        class RecordingRouter:
            async def route(self, text: str):
                events.append("route")
                return parse_route_response('{"action":"summary","period":null,"query":null}')

            async def unload(self) -> None:
                case.assertTrue(lock.locked())
                events.append("unload")

        route = await route_under_gpu_lock(RecordingRouter(), lock, "summary")  # type: ignore[arg-type]
        events.append("action" if not lock.locked() else "action_locked")
        self.assertEqual(route.action, "summary")
        self.assertEqual(events, ["route", "unload", "action"])
