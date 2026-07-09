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

from app.services.web_search import (
    WEB_SEARCH_TOOL,
    is_web_search_enabled,
    tavily_search,
)

logger = logging.getLogger(__name__)

# Максимальное число итераций агентного цикла с инструментами
MAX_TOOL_ITERATIONS = 4

# Подстроки в тексте ошибки, указывающие на неподдержку tool/function calling эндпоинтом
_TOOLS_UNSUPPORTED_MARKERS = (
    "tool",
    "function call",
    "function_call",
    "functions",
    "tool_choice",
    "does not support",
    "not supported",
    "unsupported",
    "unknown parameter",
    "unrecognized",
)


def _looks_like_tools_unsupported(error: Exception) -> bool:
    """
    Эвристика: похоже ли исключение на ошибку неподдержки инструментов эндпоинтом.

    Args:
        error: Пойманное исключение.

    Returns:
        True если ошибка вероятно связана с неподдержкой tools/function calling.
    """
    text = str(error).lower()
    # Требуем упоминание tools/functions вместе с признаком неподдержки/невалидного параметра
    mentions_tools = any(
        m in text for m in ("tool", "function")
    )
    mentions_unsupported = any(
        m in text
        for m in (
            "does not support",
            "not supported",
            "unsupported",
            "unknown parameter",
            "unrecognized",
            "invalid",
            "no such",
        )
    )
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
        temperature: float = 0.7,
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
            
            response = await acompletion(
                model=normalized_model,
                messages=messages,
                api_base=self.base_url,
                api_key=self.api_key,
                stream=True,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=self.timeout,
            )

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
        temperature: float = 0.7,
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
            
            response = await acompletion(
                model=normalized_model,
                messages=messages,
                api_base=self.base_url,
                api_key=self.api_key,
                stream=False,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=self.timeout,
            )

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
        temperature: float,
        max_tokens: int | None,
        use_tools: bool,
    ) -> Any:
        """
        Обёртка над acompletion (не-стриминг) с опциональной передачей tools.

        Args:
            model: Уже нормализованное имя модели.
            messages: История сообщений.
            temperature: Температура.
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
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=self.timeout,
        )
        if use_tools:
            kwargs["tools"] = [WEB_SEARCH_TOOL]
            kwargs["tool_choice"] = "auto"

        return await acompletion(**kwargs)

    async def agentic_completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
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
                        f"Эндпоинт не поддерживает tool calling ({e}). "
                        f"Fallback на обычный вызов без инструментов."
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
                return message.content or ""

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
        Выполняет один tool_call и возвращает строковый результат для role="tool".

        Args:
            tool_call: Объект tool_call из ответа модели.
            on_search: Опциональный async-колбэк для UI-статуса поиска.

        Returns:
            Результат инструмента (строка). При неизвестном инструменте или
            битых аргументах — понятное сообщение об ошибке.
        """
        name = getattr(tool_call.function, "name", None)

        if name != "web_search":
            logger.warning(f"Запрошен неизвестный инструмент: {name}")
            return f"Ошибка: инструмент '{name}' не поддерживается."

        # Парсим аргументы (JSON с полем query)
        raw_args = getattr(tool_call.function, "arguments", "") or ""
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
                logger.warning(f"Unexpected models response format: {data}")
                return []

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error fetching models: {e.response.status_code} - {e.response.text}")
            raise
        except httpx.TimeoutException:
            logger.error(f"Timeout fetching models from {url}")
            raise
        except Exception as e:
            logger.error(f"Error fetching models: {e}", exc_info=True)
            raise
