"""
Сервис для работы с LLM API через litellm с поддержкой streaming.
Использует litellm для универсальной работы с различными OpenAI-совместимыми эндпоинтами.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from litellm import acompletion

logger = logging.getLogger(__name__)


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
