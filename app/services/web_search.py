"""
Сервис веб-поиска через провайдер Tavily.

Использует прямой REST-вызов Tavily API через httpx (без тяжёлого SDK).
Предоставляет:
- tavily_search() — async-функция выполнения поиска, возвращает компактный текст для модели.
- is_web_search_enabled() — проверка, включён ли веб-поиск (задан ли TAVILY_API_KEY).
- WEB_SEARCH_TOOL — OpenAI-совместимая tool-схема для tool calling.
"""

import logging

import httpx

from app.config.settings import settings

logger = logging.getLogger(__name__)

# URL Tavily REST API
TAVILY_API_URL = "https://api.tavily.com/search"

# Таймаут запроса к Tavily (секунды)
TAVILY_TIMEOUT = 15

# Максимальный размер компактного результата, передаваемого модели (символы)
MAX_RESULT_CHARS = 4000

# OpenAI-совместимая tool-схема для tool calling
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for up-to-date information. Use when the user asks "
            "about recent events, current facts, or anything you might not know "
            "or be unsure about."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                }
            },
            "required": ["query"],
        },
    },
}


def is_web_search_enabled() -> bool:
    """
    Проверяет, включён ли веб-поиск.

    Returns:
        True если задан TAVILY_API_KEY, иначе False.
    """
    return bool(settings.TAVILY_API_KEY)


async def tavily_search(query: str, max_results: int = 5) -> str:
    """
    Выполняет веб-поиск через Tavily API и возвращает компактный текст результатов.

    Args:
        query: Поисковый запрос.
        max_results: Максимальное количество результатов (по умолчанию 5).

    Returns:
        Компактный текст с ответом (если есть) и списком результатов (title, url,
        короткий snippet). При ошибке — понятная строка об ошибке (модель сможет
        её учесть при формировании ответа).
    """
    if not settings.TAVILY_API_KEY:
        logger.warning("tavily_search вызван без TAVILY_API_KEY")
        return "Ошибка веб-поиска: ключ Tavily не настроен. Веб-поиск недоступен."

    payload = {
        "api_key": settings.TAVILY_API_KEY,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": True,
    }

    try:
        async with httpx.AsyncClient(timeout=TAVILY_TIMEOUT) as client:
            response = await client.post(TAVILY_API_URL, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.TimeoutException:
        logger.error(f"Таймаут веб-поиска Tavily для запроса: {query!r}")
        return f"Ошибка веб-поиска: превышен таймаут запроса ({TAVILY_TIMEOUT}с)."
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP ошибка веб-поиска Tavily: {e.response.status_code} - {e.response.text}"
        )
        return (
            f"Ошибка веб-поиска: сервис вернул статус {e.response.status_code}. "
            "Попробуйте ответить на основе имеющихся знаний."
        )
    except Exception as e:
        logger.error(f"Ошибка веб-поиска Tavily: {e}", exc_info=True)
        return f"Ошибка веб-поиска: {e}. Попробуйте ответить на основе имеющихся знаний."

    return _format_results(data, query)


def _format_results(data: dict, query: str) -> str:
    """
    Форматирует ответ Tavily в компактный текст для передачи модели.

    Args:
        data: JSON-ответ Tavily.
        query: Исходный запрос (для контекста).

    Returns:
        Компактный текст (answer + список результатов), обрезанный до MAX_RESULT_CHARS.
    """
    parts: list[str] = [f"Результаты веб-поиска по запросу: {query}"]

    answer = data.get("answer")
    if answer:
        parts.append(f"\nКраткий ответ: {answer}")

    results = data.get("results") or []
    if results:
        parts.append("\nИсточники:")
        for i, item in enumerate(results, start=1):
            title = (item.get("title") or "").strip()
            url = (item.get("url") or "").strip()
            content = (item.get("content") or "").strip()
            # Ограничиваем каждый snippet, чтобы не раздувать контекст
            if len(content) > 500:
                content = content[:500].rstrip() + "…"
            entry = f"{i}. {title}\n   URL: {url}"
            if content:
                entry += f"\n   {content}"
            parts.append(entry)
    elif not answer:
        parts.append("\nПо запросу ничего не найдено.")

    result_text = "\n".join(parts)

    # Ограничиваем общий размер, чтобы не раздувать контекст модели
    if len(result_text) > MAX_RESULT_CHARS:
        result_text = result_text[:MAX_RESULT_CHARS].rstrip() + "\n…[результаты обрезаны]"

    return result_text
