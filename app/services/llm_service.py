"""
Сервис для работы с LLM API через litellm с поддержкой streaming.
Использует litellm для универсальной работы с различными OpenAI-совместимыми эндпоинтами.
Поддерживает агентный цикл с tool calling (веб-поиск через Tavily).
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

import httpx
from litellm import acompletion

from app.config import settings

from app.services.web_search import (
    WEB_SEARCH_TOOL,
    is_web_search_enabled,
    tavily_search,
)

logger = logging.getLogger(__name__)

# Максимальное число итераций агентного цикла с инструментами
MAX_TOOL_ITERATIONS = 4

# Признаки НЕподдержки tool/function calling эндпоинтом.
# Требуем одновременно упоминание tools/functions И явную формулировку неподдержки.
# Список намеренно узкий, чтобы не срабатывать ложно на любые ошибки со словом "tool"
# (например обычные 4xx/5xx или "invalid" в несвязанном контексте) и не откатывать
# на путь без инструментов, когда эндпоинт их на самом деле поддерживает.
_TOOLS_KEYWORDS = ("tool", "function")
_UNSUPPORTED_PHRASES = (
    "does not support",
    "not supported",
    "unsupported",
    "unknown parameter",
    "unrecognized parameter",
    "unrecognized request argument",
)


def _looks_like_tools_unsupported(error: Exception) -> bool:
    """
    Эвристика: похоже ли исключение на ЯВНУЮ ошибку неподдержки инструментов эндпоинтом.

    Срабатывает только когда в тексте ошибки есть и упоминание tools/functions,
    и явная фраза о неподдержке/неизвестном параметре. Широкие маркеры вроде
    "invalid"/"no such" убраны, чтобы избежать ложного отката на путь без tools.

    Args:
        error: Пойманное исключение.

    Returns:
        True если ошибка явно связана с неподдержкой tools/function calling.
    """
    text = str(error).lower()
    mentions_tools = any(m in text for m in _TOOLS_KEYWORDS)
    mentions_unsupported = any(p in text for p in _UNSUPPORTED_PHRASES)
    return mentions_tools and mentions_unsupported


class LLMService:
    """Сервис для streaming-вызовов LLM через litellm"""

    def __init__(self, base_url: str, api_key: str, timeout: int = 120):
        """
        Args:
            base_url: Базовый URL эндпоинта (например, https://api.openai.com)
            api_key: API ключ
            timeout: Таймаут запроса в секундах
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @staticmethod
    def _resolve_temperature(temperature: float | None) -> float | None:
        """
        Определяет фактическое значение temperature для запроса.

        По умолчанию (temperature is None) берётся значение из настройки
        LLM_TEMPERATURE, которая тоже по умолчанию None. Итог: если ни явно,
        ни в настройках temperature не задана — параметр НЕ должен попасть
        в запрос (некоторые модели, напр. Claude Opus 4, возвращают ошибку
        "temperature is deprecated for this model").

        Args:
            temperature: Явно переданное значение или None.

        Returns:
            float для передачи в acompletion, либо None — тогда параметр
            не передаётся вовсе.
        """
        if temperature is not None:
            return temperature
        return settings.LLM_TEMPERATURE

    @staticmethod
    def _normalize_model(model_name: str) -> str:
        """
        Нормализует имя модели для litellm.
        Для произвольных OpenAI-совместимых эндпоинтов добавляет префикс openai/.
        
        Args:
            model_name: Исходное имя модели
            
        Returns:
            Нормализованное имя модели с префиксом провайдера
        """
        # Если модель уже содержит префикс провайдера (формат provider/model), оставляем как есть
        if "/" in model_name:
            return model_name
        
        # Иначе добавляем префикс openai/ для OpenAI-совместимых эндпоинтов
        return f"openai/{model_name}"

    async def stream_chat_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Streaming генерация ответа от LLM.

        Args:
            model: Название модели
            messages: История сообщений в формате [{"role": "user", "content": "..."}]
            temperature: Температура генерации
            max_tokens: Максимальное количество токенов в ответе

        Yields:
            Токены ответа по мере их генерации

        Raises:
            httpx.TimeoutException: При превышении таймаута
            Exception: При других ошибках API
        """
        try:
            # Нормализуем имя модели для litellm (добавляем префикс openai/ для OpenAI-совместимых API)
            normalized_model = self._normalize_model(model)

            kwargs: dict[str, Any] = dict(
                model=normalized_model,
                messages=messages,
                api_base=self.base_url,
                api_key=self.api_key,
                stream=True,
                max_tokens=max_tokens,
                timeout=self.timeout,
            )
            # temperature передаём ТОЛЬКО если он явно определён (иначе некоторые
            # модели, напр. Claude Opus 4, падают с "temperature is deprecated").
            resolved_temperature = self._resolve_temperature(temperature)
            if resolved_temperature is not None:
                kwargs["temperature"] = resolved_temperature

            response = await acompletion(**kwargs)

            # Стриминг токенов
            async for chunk in response:
                # litellm возвращает chunks в формате OpenAI
                if hasattr(chunk, "choices") and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if hasattr(delta, "content") and delta.content:
                        yield delta.content

        except asyncio.TimeoutError:
            logger.error(f"LLM request timeout after {self.timeout}s")
            raise
        except Exception as e:
            logger.error(f"LLM streaming error: {e}", exc_info=True)
            raise

    async def get_completion(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """
        Не-streaming генерация (для summarization или других задач).

        Args:
            model: Название модели
            messages: История сообщений
            temperature: Температура
            max_tokens: Максимум токенов

        Returns:
            Полный текст ответа
        """
        try:
            # Нормализуем имя модели для litellm (добавляем префикс openai/ для OpenAI-совместимых API)
            normalized_model = self._normalize_model(model)

            kwargs: dict[str, Any] = dict(
                model=normalized_model,
                messages=messages,
                api_base=self.base_url,
                api_key=self.api_key,
                stream=False,
                max_tokens=max_tokens,
                timeout=self.timeout,
            )
            resolved_temperature = self._resolve_temperature(temperature)
            if resolved_temperature is not None:
                kwargs["temperature"] = resolved_temperature

            response = await acompletion(**kwargs)

            return response.choices[0].message.content

        except asyncio.TimeoutError:
            logger.error(f"LLM request timeout after {self.timeout}s")
            raise
        except Exception as e:
            logger.error(f"LLM completion error: {e}", exc_info=True)
            raise

    async def _acompletion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        use_tools: bool,
    ) -> Any:
        """
        Обёртка над acompletion (не-стриминг) с опциональной передачей tools.

        Args:
            model: Уже нормализованное имя модели.
            messages: История сообщений.
            temperature: Температура (None — параметр не передаётся в запрос).
            max_tokens: Максимум токенов.
            use_tools: Передавать ли tools=[web_search] и tool_choice="auto".

        Returns:
            Ответ litellm (объект в формате OpenAI).
        """
        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,
            api_base=self.base_url,
            api_key=self.api_key,
            stream=False,
            max_tokens=max_tokens,
            timeout=self.timeout,
        )
        resolved_temperature = self._resolve_temperature(temperature)
        if resolved_temperature is not None:
            kwargs["temperature"] = resolved_temperature
        if use_tools:
            kwargs["tools"] = [WEB_SEARCH_TOOL]
            kwargs["tool_choice"] = "auto"

        return await acompletion(**kwargs)

    async def _astream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
        use_tools: bool,
    ) -> Any:
        """
        Обёртка над acompletion (стриминг) с опциональной передачей tools.

        Args:
            model: Уже нормализованное имя модели.
            messages: История сообщений.
            temperature: Температура (None — параметр не передаётся в запрос).
            max_tokens: Максимум токенов.
            use_tools: Передавать ли tools=[web_search] и tool_choice="auto".

        Returns:
            Async-итератор чанков litellm (в формате OpenAI).
        """
        kwargs: dict[str, Any] = dict(
            model=model,
            messages=messages,
            api_base=self.base_url,
            api_key=self.api_key,
            stream=True,
            max_tokens=max_tokens,
            timeout=self.timeout,
        )
        resolved_temperature = self._resolve_temperature(temperature)
        if resolved_temperature is not None:
            kwargs["temperature"] = resolved_temperature
        if use_tools:
            kwargs["tools"] = [WEB_SEARCH_TOOL]
            kwargs["tool_choice"] = "auto"

        return await acompletion(**kwargs)

    @staticmethod
    def _accumulate_tool_call_deltas(
        accumulator: dict[int, dict[str, Any]],
        delta_tool_calls: Any,
    ) -> None:
        """
        Аккумулирует дельты tool_calls из стрим-чанков в accumulator по index.

        litellm/OpenAI при стриминге присылают tool_calls кусочками: id/name
        обычно приходят в первом чанке для данного index, а arguments — по частям.
        Склеиваем всё по числовому index.

        Args:
            accumulator: Словарь {index: {"id", "name", "arguments"}} — мутируется на месте.
            delta_tool_calls: Список ChatCompletionDeltaToolCallChunk из delta.tool_calls.
        """
        for tc in delta_tool_calls:
            index = getattr(tc, "index", None)
            if index is None:
                index = 0
            slot = accumulator.setdefault(
                index, {"id": None, "name": None, "arguments": ""}
            )

            tc_id = getattr(tc, "id", None)
            if tc_id:
                slot["id"] = tc_id

            func = getattr(tc, "function", None)
            if func is not None:
                name = getattr(func, "name", None)
                if name:
                    slot["name"] = name
                args = getattr(func, "arguments", None)
                if args:
                    slot["arguments"] += args

    async def agentic_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_search: Callable[[str], Awaitable[None]] | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Агентный цикл с tool calling (веб-поиск), СТРИМЯЩИЙ финальный ответ.

        На каждой итерации выполняется СТРИМИНГОВЫЙ вызов с tools. Во время стрима
        аккумулируются дельты tool_calls и контента:
        - Если по завершении стрима модель запросила tool_calls — выполняем их
          (вызывая on_search для UI-статуса) и идём на следующую итерацию.
          Контентные токены этой промежуточной итерации НЕ отдаются наружу
          (это внутренние рассуждения перед вызовом инструмента).
        - Если tool_calls нет — это финальный ответ, который уже стримился наружу
          по мере поступления контентных дельт. Дополнительных вызовов нет.

        Если веб-поиск выключен — просто обычный стриминг без инструментов.
        Если эндпоинт не поддерживает tools — graceful fallback на стриминг без tools.

        Args:
            model: Название модели.
            messages: История сообщений в формате OpenAI.
            temperature: Температура генерации.
            max_tokens: Максимум токенов.
            on_search: Опциональный async-колбэк, вызывается с текстом запроса
                перед выполнением веб-поиска (для обновления статуса в UI).

        Yields:
            Токены финального ответа ассистента по мере генерации.
        """
        normalized_model = self._normalize_model(model)

        # Веб-поиск выключен — обычный стриминг без tools
        if not is_web_search_enabled():
            async for token in self.stream_chat_completion(
                model, messages, temperature, max_tokens
            ):
                yield token
            return

        logger.info(
            f"agentic_stream: web search enabled, streaming with tools=[web_search] "
            f"tool_choice=auto (model={normalized_model})"
        )

        working_messages: list[dict[str, Any]] = list(messages)
        tools_supported = True

        for iteration in range(MAX_TOOL_ITERATIONS):
            content_buffer = ""
            tool_calls_acc: dict[int, dict[str, Any]] = {}

            try:
                stream = await self._astream(
                    normalized_model,
                    working_messages,
                    temperature,
                    max_tokens,
                    use_tools=tools_supported,
                )
            except Exception as e:
                if tools_supported and _looks_like_tools_unsupported(e):
                    logger.warning(
                        f"agentic_stream: tools-unsupported fallback СРАБОТАЛ — "
                        f"эндпоинт, похоже, не поддерживает tool calling. Причина: {e}. "
                        f"Стриминг БЕЗ инструментов."
                    )
                    tools_supported = False
                    async for token in self.stream_chat_completion(
                        model, working_messages, temperature, max_tokens
                    ):
                        yield token
                    return
                raise

            # Собираем стрим текущей итерации.
            # Заранее неизвестно, будет ли tool_call. Стратегия инкрементальной отдачи:
            # пока в стриме НЕ появилось ни одной tool_call-дельты — контентные дельты
            # отдаём наружу немедленно (это финальный ответ, стримим по мере генерации).
            # Как только пришла первая tool_call-дельта — переключаемся в режим
            # накопления: контент больше не yield'им (это внутренние рассуждения перед
            # вызовом инструмента), а копим его в буфер для сообщения ассистента.
            # Модели, вызывающие инструменты, шлют tool_calls в начале стрима (до
            # пользовательского контента), поэтому «протечки» промежуточного контента
            # в UI не происходит.
            tool_call_seen = False
            async for chunk in stream:
                if not (hasattr(chunk, "choices") and len(chunk.choices) > 0):
                    continue
                delta = chunk.choices[0].delta

                delta_tool_calls = getattr(delta, "tool_calls", None)
                if delta_tool_calls:
                    tool_call_seen = True
                    self._accumulate_tool_call_deltas(tool_calls_acc, delta_tool_calls)

                content = getattr(delta, "content", None)
                if content:
                    content_buffer += content
                    # Пока инструмент не запрошен — отдаём контент инкрементально.
                    if not tool_call_seen:
                        yield content

            # Итерация без tool_calls — это финальный ответ.
            if not tool_calls_acc:
                if tools_supported:
                    logger.info(
                        f"agentic_stream: no tool_calls (iteration {iteration + 1}) "
                        f"— final answer streamed incrementally ({len(content_buffer)} chars total)"
                    )
                # Контент уже был отдан инкрементально в цикле выше — повторный yield не нужен.
                return

            logger.info(
                f"agentic_stream: LLM requested tool_calls: {len(tool_calls_acc)} "
                f"(iteration {iteration + 1})"
            )

            # Добавляем сообщение ассистента с tool_calls (в порядке index)
            ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": content_buffer or "",
                "tool_calls": [
                    {
                        "id": slot["id"],
                        "type": "function",
                        "function": {
                            "name": slot["name"],
                            "arguments": slot["arguments"] or "{}",
                        },
                    }
                    for slot in ordered
                ],
            }
            working_messages.append(assistant_msg)

            # Выполняем каждый tool_call, добавляя role="tool" результаты
            for slot in ordered:
                tool_result = await self._handle_tool_call_args(
                    slot["name"], slot["arguments"], on_search
                )
                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": slot["id"],
                        "name": slot["name"],
                        "content": tool_result,
                    }
                )

        # Достигнут лимит итераций — финальный стриминг БЕЗ инструментов
        logger.warning(
            f"agentic_stream: достигнут лимит итераций ({MAX_TOOL_ITERATIONS}). "
            f"Финальный стриминг без инструментов."
        )
        async for token in self.stream_chat_completion(
            model, working_messages, temperature, max_tokens
        ):
            yield token

    async def agentic_completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_search: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """
        Агентный цикл с tool calling (веб-поиск через Tavily).

        Если веб-поиск включён (задан TAVILY_API_KEY), модель может сама решать,
        когда вызывать инструмент web_search. Цикл выполняется до MAX_TOOL_ITERATIONS
        итераций. Если эндпоинт не поддерживает tools — graceful fallback на
        обычный не-стриминговый вызов без инструментов.

        Args:
            model: Название модели.
            messages: История сообщений в формате OpenAI.
            temperature: Температура генерации.
            max_tokens: Максимум токенов.
            on_search: Опциональный async-колбэк, вызывается с текстом запроса
                перед выполнением веб-поиска (для обновления статуса в UI).

        Returns:
            Финальный текст ответа ассистента.
        """
        normalized_model = self._normalize_model(model)

        # Если веб-поиск выключен — просто обычный не-стриминговый вызов
        if not is_web_search_enabled():
            response = await self._acompletion(
                normalized_model, messages, temperature, max_tokens, use_tools=False
            )
            return response.choices[0].message.content or ""

        logger.info(
            f"agentic_completion: web search enabled, passing tools=[web_search] "
            f"with tool_choice=auto (model={normalized_model})"
        )

        # Рабочая копия истории (чтобы не мутировать переданный список)
        working_messages: list[dict[str, Any]] = list(messages)
        tools_supported = True

        for iteration in range(MAX_TOOL_ITERATIONS):
            try:
                response = await self._acompletion(
                    normalized_model,
                    working_messages,
                    temperature,
                    max_tokens,
                    use_tools=tools_supported,
                )
            except Exception as e:
                if tools_supported and _looks_like_tools_unsupported(e):
                    logger.warning(
                        f"agentic_completion: tools-unsupported fallback СРАБОТАЛ — "
                        f"эндпоинт, похоже, не поддерживает tool calling. Причина: {e}. "
                        f"Повтор запроса БЕЗ инструментов."
                    )
                    tools_supported = False
                    response = await self._acompletion(
                        normalized_model,
                        working_messages,
                        temperature,
                        max_tokens,
                        use_tools=False,
                    )
                else:
                    raise

            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)

            # Нет tool_calls — это финальный ответ
            if not tool_calls:
                if tools_supported:
                    logger.info(
                        f"agentic_completion: LLM requested tool_calls: 0 "
                        f"(iteration {iteration + 1}) — returning final answer"
                    )
                return message.content or ""

            logger.info(
                f"agentic_completion: LLM requested tool_calls: {len(tool_calls)} "
                f"(iteration {iteration + 1})"
            )

            # Есть tool_calls — добавляем сообщение ассистента с tool_calls
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
            working_messages.append(assistant_msg)

            # Обрабатываем каждый tool_call, добавляя role="tool" результаты
            for tc in tool_calls:
                tool_result = await self._handle_tool_call(tc, on_search)
                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": tool_result,
                    }
                )

        # Достигнут лимит итераций — просим модель ответить БЕЗ инструментов
        logger.warning(
            f"Достигнут лимит итераций агентного цикла ({MAX_TOOL_ITERATIONS}). "
            f"Финальный запрос без инструментов."
        )
        final_response = await self._acompletion(
            normalized_model, working_messages, temperature, max_tokens, use_tools=False
        )
        return final_response.choices[0].message.content or ""

    async def _handle_tool_call(
        self,
        tool_call: Any,
        on_search: Callable[[str], Awaitable[None]] | None,
    ) -> str:
        """
        Выполняет один tool_call (объект) и возвращает строковый результат.

        Обёртка над _handle_tool_call_args для не-стримингового пути, где
        доступен объект tool_call с полями function.name/function.arguments.

        Args:
            tool_call: Объект tool_call из ответа модели.
            on_search: Опциональный async-колбэк для UI-статуса поиска.

        Returns:
            Результат инструмента (строка).
        """
        name = getattr(tool_call.function, "name", None)
        raw_args = getattr(tool_call.function, "arguments", "") or ""
        return await self._handle_tool_call_args(name, raw_args, on_search)

    async def _handle_tool_call_args(
        self,
        name: str | None,
        raw_args: str,
        on_search: Callable[[str], Awaitable[None]] | None,
    ) -> str:
        """
        Выполняет инструмент по имени и сырым аргументам, возвращает результат
        для role="tool". Используется как стриминговым (аккумулированные дельты),
        так и не-стриминговым путём.

        Args:
            name: Имя инструмента.
            raw_args: Сырая строка аргументов (JSON с полем query).
            on_search: Опциональный async-колбэк для UI-статуса поиска.

        Returns:
            Результат инструмента (строка). При неизвестном инструменте или
            битых аргументах — понятное сообщение об ошибке.
        """
        if name != "web_search":
            logger.warning(f"Запрошен неизвестный инструмент: {name}")
            return f"Ошибка: инструмент '{name}' не поддерживается."

        raw_args = raw_args or ""
        try:
            args = json.loads(raw_args) if raw_args else {}
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Не удалось распарсить аргументы web_search: {raw_args!r} ({e})")
            return "Ошибка: некорректные аргументы для web_search (ожидался JSON с полем 'query')."

        query = (args.get("query") or "").strip()
        if not query:
            return "Ошибка: пустой поисковый запрос."

        logger.info(f"Выполняется web_search: {query!r}")

        if on_search is not None:
            try:
                await on_search(query)
            except Exception as cb_err:
                logger.warning(f"Ошибка колбэка on_search: {cb_err}")

        return await tavily_search(query)


async def fetch_available_models(base_url: str, api_key: str, timeout: int = 30) -> list[dict[str, Any]]:
    """
    Получает список доступных моделей через GET /v1/models.

    Args:
        base_url: Базовый URL эндпоинта
        api_key: API ключ
        timeout: Таймаут запроса

    Returns:
        Список моделей в формате [{"id": "model-name", "object": "model", ...}]

    Raises:
        httpx.HTTPStatusError: При ошибках HTTP
        httpx.TimeoutException: При таймауте
    """
    url = f"{base_url.rstrip('/')}/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()

            # OpenAI-совместимый формат: {"data": [...], "object": "list"}
            if isinstance(data, dict) and "data" in data:
                return data["data"]
            elif isinstance(data, list):
                return data
            else:
                logger.warning("Unexpected models response format (payload omitted)")
                return []

        except httpx.HTTPStatusError as e:
            # Не логируем URL, заголовки или response body: они могут содержать
            # credentials либо чувствительные данные провайдера.
            logger.error(
                "HTTP error fetching models: status=%s",
                e.response.status_code,
            )
            raise
        except httpx.TimeoutException:
            logger.error("Timeout fetching models")
            raise
        except Exception as e:
            logger.error(
                "Error fetching models: type=%s",
                type(e).__name__,
            )
            raise
