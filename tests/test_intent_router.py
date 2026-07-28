import asyncio
import unittest

import tg_summary_bot.bot as bot_module
import tg_summary_bot.intent_router as intent_router
from tg_summary_bot.intent_router import (
    IntentRoute,
    IntentRouter,
    infer_profile_query,
    is_casino_request,
    is_joke_request,
    parse_route_response,
)
from tg_summary_bot.bot import (
    has_bot_command_entity,
    route_under_gpu_lock,
    should_generate_surprise_meme,
)


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

    def test_missing_wiki_query_has_a_stable_reason(self) -> None:
        route = parse_route_response('{"action":"wiki","period":null,"query":""}')
        self.assertFalse(route.valid)
        self.assertEqual(route.reason, "missing_wiki_query")

    def test_infers_named_profile_target_but_not_self_profile(self) -> None:
        self.assertEqual(infer_profile_query("покажи профиль Артёма"), "Артёма")
        self.assertIsNone(infer_profile_query("покажи мой профиль"))

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

    def test_casino_requires_null_model_fields(self) -> None:
        self.assertFalse(parse_route_response('{"action":"casino","period":"1h","query":null}').valid)
        self.assertFalse(parse_route_response('{"action":"casino","period":null,"query":"100"}').valid)
        route = parse_route_response('{"action":"casino","period":null,"query":null}')
        self.assertTrue(route.valid)
        self.assertEqual(route.action, "casino")


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

    async def test_rejects_profile_route_without_an_explicit_profile_request(self) -> None:
        router = IntentRouter(FakeLLM('{"action":"profile_show","period":null,"query":null}'))  # type: ignore[arg-type]
        route = await router.route("расскажи про себя, какая ты модель?")
        self.assertFalse(route.valid)
        self.assertEqual(route.reason, "profile_not_explicit")

    async def test_accepts_explicit_profile_request(self) -> None:
        router = IntentRouter(FakeLLM('{"action":"profile_show","period":null,"query":null}'))  # type: ignore[arg-type]
        route = await router.route("покажи профиль Артёма")
        self.assertTrue(route.valid)
        self.assertEqual(route.action, "profile_show")
        self.assertEqual(route.query, "Артёма")

    async def test_casino_needs_action_verb_and_object(self) -> None:
        router = IntentRouter(FakeLLM('{"action":"casino","period":null,"query":null}'))  # type: ignore[arg-type]
        for text in (
            "Реле, прокрути казино",
            "Реле, крутань слот",
            "Реле, сыграй в слоты",
            "Реле, крутануть казик",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_casino_request(text))
                self.assertEqual((await router.route(text)).action, "casino")
        for text in ("что ты думаешь о казино?", "объясни правила слотов", "вчера было казино"):
            with self.subTest(text=text):
                self.assertFalse(is_casino_request(text))
                self.assertEqual((await router.route(text)).reason, "casino_not_explicit")

    def test_production_router_constructor_is_imported(self) -> None:
        self.assertIs(bot_module.IntentRouter, IntentRouter)

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


class SurpriseMemePolicyTests(unittest.TestCase):
    def test_only_valid_question_can_trigger_surprise_meme(self) -> None:
        valid_question = parse_route_response(
            '{"action":"question","period":null,"query":null}'
        )
        self.assertTrue(
            should_generate_surprise_meme(
                route=valid_question,
                request="расскажи шутку",
                has_image=True,
                random_value=0.0,
            )
        )
        for route in (
            intent_router.fallback_route("invalid_json"),
            intent_router.fallback_route("meme_not_explicit"),
            parse_route_response('{"action":"none","period":null,"query":null}'),
            parse_route_response('{"action":"meme","period":null,"query":null}'),
        ):
            with self.subTest(route=route):
                self.assertFalse(
                    should_generate_surprise_meme(
                        route=route,
                        request="расскажи шутку",
                        has_image=True,
                        random_value=0.0,
                    )
                )

        self.assertFalse(
            should_generate_surprise_meme(
                route=IntentRoute(action="question", valid=True, reason="custom"),
                request="пошути",
                has_image=True,
                random_value=0.0,
            )
        )

    def test_requires_joke_image_and_probability(self) -> None:
        route = parse_route_response('{"action":"question","period":null,"query":null}')
        self.assertFalse(
            should_generate_surprise_meme(
                route=route,
                request="ответь на вопрос",
                has_image=True,
                random_value=0.0,
            )
        )
        self.assertFalse(
            should_generate_surprise_meme(
                route=route,
                request="пошути",
                has_image=False,
                random_value=0.0,
            )
        )
        self.assertFalse(
            should_generate_surprise_meme(
                route=route,
                request="пошути",
                has_image=True,
                random_value=0.5,
            )
        )


class BotCommandEntityTests(unittest.TestCase):
    def test_detects_commands_in_text_and_media_captions(self) -> None:
        entity = type("Entity", (), {"type": "bot_command"})()
        text_message = type(
            "TextMessage",
            (),
            {"text": "/image", "entities": [entity], "caption_entities": None},
        )()
        caption_message = type(
            "CaptionMessage",
            (),
            {"text": None, "entities": None, "caption_entities": [entity]},
        )()
        self.assertTrue(has_bot_command_entity(text_message))  # type: ignore[arg-type]
        self.assertTrue(has_bot_command_entity(caption_message))  # type: ignore[arg-type]
