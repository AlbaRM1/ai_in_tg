"""
Сервис управления контекстом чата: подсчёт токенов, sliding window, сжатие истории.
"""

import logging
from typing import Any

from litellm import token_counter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import ChatSession, Message

logger = logging.getLogger(__name__)


def estimate_tokens_fallback(text: str) -> int:
    """
    Fallback оценка количества токенов (если litellm.token_counter не работает).
    Грубая оценка: ~4 символа = 1 токен.

    Args:
        text: Текст для оценки

    Returns:
        Примерное количество токенов
    """
    return len(text) // 4


def count_tokens(model: str, messages: list[dict[str, Any]]) -> int:
    """
    Подсчёт токенов с использованием litellm.token_counter (model-aware).
    Поддерживает мультимодальный контент (list of parts).

    Args:
        model: Название модели (для правильного токенизатора)
        messages: Список сообщений в формате [{"role": "user", "content": "..."}]
                  content может быть строкой или списком parts (мультимодальный)

    Returns:
        Количество токенов
    """
    try:
        # litellm.token_counter поддерживает разные модели
        return token_counter(model=model, messages=messages)
    except Exception as e:
        logger.warning(f"token_counter failed for model {model}: {e}, using fallback")
        # Fallback: извлекаем текст из content (строка или list)
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                # Мультимодальный контент: суммируем только текстовые части
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total_chars += len(part.get("text", ""))
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        # Фиксированная оценка для изображений (OpenAI использует ~765 токенов)
                        total_chars += 765 * 4  # ~3060 символов = 765 токенов
        return estimate_tokens_fallback(total_chars)


async def load_session_history(
    session: AsyncSession,
    chat_session: ChatSession,
    max_tokens: int | None = None,
) -> list[dict[str, Any]]:
    """
    Загружает историю сообщений сессии.
    Поддерживает мультимодальный контент (content_parts).

    Args:
        session: DB сессия
        chat_session: Сессия чата
        max_tokens: Максимальное количество токенов (если None — из settings)

    Returns:
        История в формате [{"role": "system", "content": "..."}, ...]
        content может быть строкой или списком parts
    """
    if max_tokens is None:
        max_tokens = settings.MAX_CONTEXT_TOKENS

    # Загружаем все сообщения сессии (отсортированы по created_at в модели)
    result = await session.execute(
        select(Message)
        .where(Message.session_id == chat_session.id)
        .order_by(Message.created_at)
    )
    messages_db = result.scalars().all()

    # Формируем историю
    history: list[dict[str, Any]] = []
    
    # Всегда добавляем system prompt первым
    if chat_session.system_prompt:
        history.append({"role": "system", "content": chat_session.system_prompt})

    # Добавляем сообщения из БД
    for msg in messages_db:
        # Если есть content_parts (мультимодальный формат) — используем его
        if msg.content_parts:
            history.append({"role": msg.role, "content": msg.content_parts})
        else:
            # Иначе используем обычное текстовое поле
            history.append({"role": msg.role, "content": msg.content})

    # Проверяем, не превышаем ли лимит токенов
    total_tokens = count_tokens(chat_session.model, history)

    if total_tokens > max_tokens:
        logger.warning(
            f"Session {chat_session.id} exceeds token limit: {total_tokens} > {max_tokens}. Compressing..."
        )
        history = await compress_history(
            session=session,
            chat_session=chat_session,
            history=history,
            max_tokens=max_tokens,
        )

    return history


async def compress_history(
    session: AsyncSession,
    chat_session: ChatSession,
    history: list[dict[str, Any]],
    max_tokens: int,
) -> list[dict[str, Any]]:
    """
    Сжимает историю, если она превышает лимит токенов.
    Стратегия: удаляем самые старые сообщения, сохраняя system prompt.

    TODO: Для более умного сжатия можно использовать summarization через быструю модель.

    Args:
        session: DB сессия
        chat_session: Сессия чата
        history: Текущая история
        max_tokens: Максимальный лимит токенов

    Returns:
        Сжатая история
    """
    # Сохраняем system prompt
    system_prompt = None
    if history and history[0]["role"] == "system":
        system_prompt = history[0]
        history = history[1:]

    # Удаляем старые сообщения, пока не уложимся в лимит
    while history:
        current_tokens = count_tokens(
            chat_session.model,
            ([system_prompt] if system_prompt else []) + history,
        )

        if current_tokens <= max_tokens:
            break

        # Удаляем самое старое сообщение
        removed = history.pop(0)
        logger.info(f"Removed oldest message from session {chat_session.id}: {removed['role']}")

        # Удаляем из БД (опционально, можно оставить для истории)
        # В текущей реализации просто не грузим их в memory

    # Возвращаем system prompt + оставшуюся историю
    result = []
    if system_prompt:
        result.append(system_prompt)
    result.extend(history)

    return result


async def add_message_to_session(
    session: AsyncSession,
    chat_session: ChatSession,
    role: str,
    content: str,
    content_parts: list | None = None,
) -> Message:
    """
    Добавляет сообщение в сессию с подсчётом токенов.
    Поддерживает мультимодальный контент (content_parts).

    Args:
        session: DB сессия
        chat_session: Сессия чата
        role: Роль (user/assistant)
        content: Текстовое содержимое (или текстовая выжимка для мультимодального)
        content_parts: Опциональный мультимодальный контент (список parts)

    Returns:
        Созданное сообщение
    """
    # Подсчитываем токены для этого сообщения
    try:
        if content_parts:
            # Для мультимодального контента: считаем токены по parts
            token_count = 0
            
            # Текстовые части
            text_parts = [p.get("text", "") for p in content_parts if p.get("type") == "text"]
            if text_parts:
                text_content = " ".join(text_parts)
                token_count += count_tokens(
                    chat_session.model,
                    [{"role": role, "content": text_content}],
                )
            
            # Изображения (фиксированная оценка: ~765 токенов на изображение)
            image_count = sum(1 for p in content_parts if p.get("type") == "image_url")
            token_count += image_count * 765
            
            logger.debug(
                f"Multimodal message tokens: {token_count} "
                f"(text + {image_count} images)"
            )
        else:
            # Обычное текстовое сообщение
            token_count = count_tokens(
                chat_session.model,
                [{"role": role, "content": content}],
            )
    except Exception as e:
        logger.warning(f"Token counting failed: {e}, using fallback")
        token_count = estimate_tokens_fallback(content)

    message = Message(
        session_id=chat_session.id,
        role=role,
        content=content,
        content_parts=content_parts,
        token_count=token_count,
    )

    session.add(message)
    await session.flush()  # Чтобы получить ID

    logger.debug(
        f"Added {role} message to session {chat_session.id}: {token_count} tokens"
        + (f" (multimodal)" if content_parts else "")
    )

    return message


async def ensure_context_fits(
    session: AsyncSession,
    chat_session: ChatSession,
    new_message_content: str,
    max_tokens: int | None = None,
) -> bool:
    """
    Проверяет, поместится ли новое сообщение в контекст.
    Если нет — сжимает историю.

    Args:
        session: DB сессия
        chat_session: Сессия чата
        new_message_content: Содержимое нового сообщения пользователя
        max_tokens: Лимит токенов

    Returns:
        True если всё ок, False если даже после сжатия не помещается
    """
    if max_tokens is None:
        max_tokens = settings.MAX_CONTEXT_TOKENS

    # Загружаем текущую историю
    history = await load_session_history(session, chat_session, max_tokens)

    # Добавляем новое сообщение
    test_history = history + [{"role": "user", "content": new_message_content}]

    # Считаем токены
    total_tokens = count_tokens(chat_session.model, test_history)

    if total_tokens <= max_tokens:
        return True

    logger.warning(
        f"New message would exceed limit: {total_tokens} > {max_tokens}. Attempting compression..."
    )

    # Пробуем сжать
    compressed = await compress_history(
        session=session,
        chat_session=chat_session,
        history=history,
        max_tokens=max_tokens - count_tokens(chat_session.model, [{"role": "user", "content": new_message_content}]),
    )

    # Проверяем снова
    final_history = compressed + [{"role": "user", "content": new_message_content}]
    final_tokens = count_tokens(chat_session.model, final_history)

    if final_tokens <= max_tokens:
        logger.info(f"Context compressed successfully: {total_tokens} -> {final_tokens} tokens")
        return True
    else:
        logger.error(f"Cannot fit message even after compression: {final_tokens} > {max_tokens}")
        return False
