"""Динамический runtime-контекст, добавляемый к каждому LLM-запросу."""

import logging
from datetime import datetime, timezone, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings

logger = logging.getLogger(__name__)

RUNTIME_CONTEXT_START = "[RUNTIME_CONTEXT_DATETIME]"
RUNTIME_CONTEXT_END = "[/RUNTIME_CONTEXT_DATETIME]"
_RUNTIME_CONTEXT_VALUE_PREFIX = "Текущая дата и время:"

_RUSSIAN_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_RUSSIAN_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


@lru_cache(maxsize=32)
def _resolve_timezone(timezone_name: str) -> tuple[tzinfo, str]:
    """Разрешает IANA-зону; при ошибке безопасно возвращает встроенный UTC."""
    try:
        return ZoneInfo(timezone_name), timezone_name
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        # Функция кэшируется по имени зоны, поэтому warning появляется однократно для
        # каждого ошибочного значения, а не на каждом completion. timezone.utc не
        # зависит от наличия системной IANA-базы или пакета tzdata.
        logger.warning(
            "Некорректная IANA timezone APP_TIMEZONE=%r; используется UTC",
            timezone_name,
        )
        return timezone.utc, "UTC"


def build_runtime_datetime_block(
    *,
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> str:
    """Строит однозначный системный блок с актуальными локальными датой и временем."""
    configured_name = timezone_name or settings.APP_TIMEZONE
    tz, effective_name = _resolve_timezone(configured_name)
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local_now = instant.astimezone(tz).replace(microsecond=0)

    offset = local_now.strftime("%z")
    formatted_offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC+00:00"
    human_datetime = (
        f"{local_now.day} {_RUSSIAN_MONTHS[local_now.month - 1]} "
        f"{local_now.year} года, {_RUSSIAN_WEEKDAYS[local_now.weekday()]}, "
        f"{local_now:%H:%M:%S}"
    )
    return (
        f"{RUNTIME_CONTEXT_START}\n"
        f"Текущая дата и время: {human_datetime} "
        f"({formatted_offset}, {effective_name}). "
        f"ISO 8601: {local_now.isoformat(timespec='seconds')}.\n"
        f"{RUNTIME_CONTEXT_END}"
    )


def _clean_runtime_content(content: str) -> str:
    """Удаляет полные и оборванные служебные блоки, не трогая похожий текст."""
    lines = content.splitlines()
    cleaned: list[str] = []
    index = 0

    while index < len(lines):
        marker = lines[index].strip()
        if marker == RUNTIME_CONTEXT_START:
            end_index = next(
                (
                    candidate
                    for candidate in range(index + 1, len(lines))
                    if lines[candidate].strip() == RUNTIME_CONTEXT_END
                ),
                None,
            )
            if end_index is not None:
                index = end_index + 1
                continue

            # Для оборванного start удаляем лишь marker и непосредственно следующую
            # строку нашего формата. Остаток system prompt обязан сохраниться.
            index += 1
            if (
                index < len(lines)
                and lines[index].strip().startswith(_RUNTIME_CONTEXT_VALUE_PREFIX)
            ):
                index += 1
            continue

        if marker == RUNTIME_CONTEXT_END:
            # При потерянном start удаляем непосредственно предшествующую строку
            # нашего известного формата, но не произвольный пользовательский текст.
            if cleaned and cleaned[-1].strip().startswith(_RUNTIME_CONTEXT_VALUE_PREFIX):
                cleaned.pop()
            index += 1
            continue

        cleaned.append(lines[index])
        index += 1

    return "\n".join(cleaned).strip()


def without_runtime_context(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Возвращает поверхностную копию истории без runtime-блоков в system messages."""
    result = [dict(message) for message in messages]
    for message in result:
        if message.get("role") != "system":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = _clean_runtime_content(content)
    return result


def with_runtime_context(
    messages: list[dict[str, Any]],
    *,
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Возвращает новый список сообщений с единственным актуальным runtime-блоком.

    Входной список и его словари не мутируются. Вложенные content/tool_calls
    переиспользуются без изменений. Ранее добавленные блоки удаляются, поэтому
    повторная обработка безопасна и не накапливает timestamps.
    """
    result = without_runtime_context(messages)
    runtime_block = build_runtime_datetime_block(
        timezone_name=timezone_name,
        now=now,
    )

    system_index: int | None = None
    for index, message in enumerate(result):
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            if system_index is None:
                system_index = index

    if system_index is None:
        result.insert(0, {"role": "system", "content": runtime_block})
    else:
        base_content = result[system_index].get("content", "").strip()
        result[system_index]["content"] = (
            f"{base_content}\n\n{runtime_block}" if base_content else runtime_block
        )

    return result
