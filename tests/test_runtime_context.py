"""Dependency-free smoke/unit checks for dynamic LLM runtime datetime context."""

import copy
import logging
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.llm_service import LLMService
from app.services.runtime_context import (
    RUNTIME_CONTEXT_END,
    RUNTIME_CONTEXT_START,
    _resolve_timezone,
    build_runtime_datetime_block,
    with_runtime_context,
)

_FIXED_NOW = datetime(2026, 7, 20, 17, 34, 27, tzinfo=timezone.utc)


class RuntimeContextTests(unittest.TestCase):
    def test_yekaterinburg_offset(self) -> None:
        block = build_runtime_datetime_block(
            timezone_name="Asia/Yekaterinburg",
            now=_FIXED_NOW,
        )
        self.assertIn("22:34:27", block)
        self.assertIn("понедельник", block)
        self.assertIn("UTC+05:00, Asia/Yekaterinburg", block)
        self.assertIn("2026-07-20T22:34:27+05:00", block)

    def test_utc(self) -> None:
        block = build_runtime_datetime_block(timezone_name="UTC", now=_FIXED_NOW)
        self.assertIn("17:34:27", block)
        self.assertIn("UTC+00:00, UTC", block)
        self.assertIn("2026-07-20T17:34:27+00:00", block)

    def test_invalid_timezone_falls_back_to_utc_and_logs_once(self) -> None:
        _resolve_timezone.cache_clear()
        with self.assertLogs("app.services.runtime_context", level=logging.WARNING) as logs:
            first = build_runtime_datetime_block(
                timezone_name="Invalid/Runtime_Test_Zone",
                now=_FIXED_NOW,
            )
            second = build_runtime_datetime_block(
                timezone_name="Invalid/Runtime_Test_Zone",
                now=_FIXED_NOW,
            )
        self.assertIn("UTC+00:00, UTC", first)
        self.assertEqual(first, second)
        self.assertEqual(sum("используется UTC" in line for line in logs.output), 1)

    def test_fallback_does_not_require_iana_utc_entry(self) -> None:
        _resolve_timezone.cache_clear()
        with patch(
            "app.services.runtime_context.ZoneInfo",
            side_effect=__import__("zoneinfo").ZoneInfoNotFoundError("no tzdata"),
        ):
            with self.assertLogs("app.services.runtime_context", level=logging.WARNING):
                block = build_runtime_datetime_block(
                    timezone_name="Missing/Zone",
                    now=_FIXED_NOW,
                )
        self.assertIn("UTC+00:00, UTC", block)

    def test_repeated_call_does_not_duplicate_and_does_not_mutate_input(self) -> None:
        messages = [
            {"role": "system", "content": "Base prompt"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "nested"}],
                "tool_calls": [{"function": {"arguments": "{}"}}],
            },
        ]
        original = copy.deepcopy(messages)
        first = with_runtime_context(messages, timezone_name="UTC", now=_FIXED_NOW)
        second = with_runtime_context(first, timezone_name="UTC", now=_FIXED_NOW)

        self.assertEqual(messages, original)
        self.assertEqual(first[0]["content"].count(RUNTIME_CONTEXT_START), 1)
        self.assertEqual(second[0]["content"].count(RUNTIME_CONTEXT_START), 1)
        self.assertIs(first[1]["content"], messages[1]["content"])
        self.assertIs(first[1]["tool_calls"], messages[1]["tool_calls"])

    def test_malformed_markers_are_removed_without_touching_similar_user_text(self) -> None:
        messages = [
            {
                "role": "system",
                "content": (
                    f"Base one\n{RUNTIME_CONTEXT_START}\n"
                    "Текущая дата и время: stale\nTail instruction"
                ),
            },
            {
                "role": "system",
                "content": (
                    "Base two\nТекущая дата и время: stale\n"
                    f"{RUNTIME_CONTEXT_END}\n"
                    "Keep [RUNTIME_CONTEXT_DATETIME-ish] literally"
                ),
            },
        ]
        result = with_runtime_context(messages, timezone_name="UTC", now=_FIXED_NOW)
        combined = "\n".join(message["content"] for message in result)

        self.assertEqual(combined.count(RUNTIME_CONTEXT_START), 1)
        self.assertEqual(combined.count(RUNTIME_CONTEXT_END), 1)
        self.assertNotIn("stale", combined)
        self.assertIn("Base one", combined)
        self.assertIn("Base two", combined)
        self.assertIn("Tail instruction", combined)
        self.assertIn("[RUNTIME_CONTEXT_DATETIME-ish]", combined)
        self.assertIn(RUNTIME_CONTEXT_START, result[0]["content"])
        self.assertNotIn(RUNTIME_CONTEXT_START, result[1]["content"])


class LLMBoundaryRuntimeContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_block_is_included_in_token_counting(self) -> None:
        from app.services.context_service import count_tokens

        messages = with_runtime_context(
            [{"role": "user", "content": "Hello"}],
            timezone_name="UTC",
            now=_FIXED_NOW,
        )
        with patch(
            "app.services.context_service.token_counter",
            return_value=123,
        ) as counter:
            self.assertEqual(count_tokens("test-model", messages), 123)
        sent = counter.call_args.kwargs["messages"]
        self.assertIn(RUNTIME_CONTEXT_START, sent[0]["content"])

    async def test_both_low_level_boundaries_refresh_runtime_context(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="done"))]
        )

        async def empty_stream():
            if False:
                yield None

        with (
            patch(
                "app.services.llm_service.acompletion",
                new=AsyncMock(side_effect=[response, empty_stream()]),
            ) as mocked,
            patch(
                "app.services.runtime_context.build_runtime_datetime_block",
                side_effect=[
                    f"{RUNTIME_CONTEXT_START}\nfirst\n{RUNTIME_CONTEXT_END}",
                    f"{RUNTIME_CONTEXT_START}\nsecond\n{RUNTIME_CONTEXT_END}",
                ],
            ) as builder,
        ):
            service = LLMService("https://example.test", "test-key")
            messages = [{"role": "user", "content": "Hello"}]
            await service._acompletion("openai/test", messages, None, None, False)
            await service._astream("openai/test", messages, None, None, False)

        self.assertEqual(mocked.await_count, 2)
        self.assertEqual(builder.call_count, 2)
        sent_first = mocked.await_args_list[0].kwargs["messages"]
        sent_second = mocked.await_args_list[1].kwargs["messages"]
        self.assertIn("first", sent_first[0]["content"])
        self.assertIn("second", sent_second[0]["content"])
        for sent in (sent_first, sent_second):
            self.assertEqual(sent[0]["role"], "system")
            self.assertEqual(sent[0]["content"].count(RUNTIME_CONTEXT_START), 1)
    async def test_timestamp_present_in_regular_stream_flow(self) -> None:
        input_messages = [{"role": "user", "content": "Hello"}]
        original = copy.deepcopy(input_messages)

        async def empty_stream():
            if False:
                yield None

        with patch(
            "app.services.llm_service.acompletion",
            new=AsyncMock(return_value=empty_stream()),
        ) as mocked:
            service = LLMService("https://example.test", "test-key")
            tokens = [
                token
                async for token in service.stream_chat_completion(
                    "test-model", input_messages
                )
            ]

        self.assertEqual(tokens, [])
        sent = mocked.await_args.kwargs["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn(RUNTIME_CONTEXT_START, sent[0]["content"])
        self.assertEqual(input_messages, original)

    async def test_timestamp_present_in_compression_completion_flow(self) -> None:
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="summary"))]
        )
        with patch(
            "app.services.llm_service.acompletion",
            new=AsyncMock(return_value=response),
        ) as mocked:
            service = LLMService("https://example.test", "test-key")
            result = await service.get_completion(
                "test-model",
                [{"role": "user", "content": "Compress this history"}],
            )

        self.assertEqual(result, "summary")
        sent = mocked.await_args.kwargs["messages"]
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn(RUNTIME_CONTEXT_START, sent[0]["content"])


    async def test_compression_keeps_instruction_and_excludes_runtime_from_payload(self) -> None:
        from app.services.context_service import llm_compress_history

        stale = build_runtime_datetime_block(timezone_name="UTC", now=_FIXED_NOW)
        chat_session = SimpleNamespace(id=1, model_name="test-model", model=None)
        with patch(
            "app.services.llm_service.LLMService.get_completion",
            new=AsyncMock(return_value="clean summary"),
        ) as mocked:
            summary = await llm_compress_history(
                chat_session,
                [
                    {"role": "system", "content": f"Base prompt\n\n{stale}"},
                    {"role": "user", "content": "Important fact"},
                ],
                "test-key",
                "https://example.test",
            )

        sent = mocked.await_args.kwargs["messages"]
        serialized = "\n".join(message["content"] for message in sent)
        self.assertEqual(summary, "clean summary")
        self.assertEqual(sent[0]["role"], "system")
        self.assertIn("Не включай в резюме", sent[0]["content"])
        self.assertNotIn(RUNTIME_CONTEXT_START, serialized)
        self.assertIn("Important fact", serialized)


if __name__ == "__main__":
    unittest.main()
