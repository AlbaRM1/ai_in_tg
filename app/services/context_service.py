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
from app.services.web_search import is_web_search_enabled

logger = logging.getLogger(__name__)

# Инструкция, добавляемая к системному промпту, когда включён веб-поиск (Tavily).
# Явно сообщает модели о доступном инструменте web_search и требует активно его
# использовать для актуальных/свежих данных вместо отказа «нет доступа к интернету».
WEB_SEARCH_SYSTEM_INSTRUCTION = (
    "У тебя есть инструмент web_search для поиска в интернете. "
    "ОБЯЗАТЕЛЬНО вызывай его, когда вопрос касается актуальных/свежих данных "
    "(погода, новости, курсы валют, спортивные результаты, события, цены, "
    "текущая дата/время-зависимые факты) или когда ты не уверен в ответе, либо "
    "информация могла устареть. Никогда не отказывай пользователю фразами про "
    "отсутствие доступа к интернету или актуальным данным — вместо этого вызови "
    "web_search. Формулируй конкретный поисковый запрос и отвечай на основе "
    "полученных результатов. "
    "You have a web_search tool. You MUST call it for any question about "
    "up-to-date or real-time information, or when you are unsure — do not refuse "
    "by saying you lack internet access."
)


def build_system_prompt(base_prompt: str | None) -> str:
    """
    Формирует итоговый системный промпт для отправки в LLM.

    Если веб-поиск включён (задан TAVILY_API_KEY), к базовому промпту добавляется
    инструкция активно использовать инструмент web_search. Базовый промпт из БД
    при этом не изменяется — дополнение происходит только в рантайме.

    Args:
        base_prompt: Базовый системный промпт сессии (может быть None/пустым).

    Returns:
        Итоговый системный промпт (base + инструкция про web_search при включённом поиске).
    """
    base = (base_prompt or "").strip()

    if is_web_search_enabled():
        if base:
            return f"{base}\n\n{WEB_SEARCH_SYSTEM_INSTRUCTION}"
        return WEB_SEARCH_SYSTEM_INSTRUCTION

    return base


# Доля лимита контекста, резервируемая под ответ модели и неточность оценки.
# Например, при 0.15 под историю используется 85% лимита, остальное — запас
# на генерацию ответа (иначе история влезает, но история+ответ переполняют окно).
_CONTEXT_RESERVE_RATIO = 0.15
# Абсолютный минимум лимита истории (страховка от абсурдно малых значений).
_MIN_CONTEXT_LIMIT = 1_000


def _effective_context_limit(max_tokens: int | None) -> int:
    """
    Вычисляет эффективный лимит токенов для истории с резервом под ответ.

    Args:
        max_tokens: Полный лимит контекста (если None — берётся из settings).

    Returns:
        Лимит токенов для истории (< max_tokens на величину резерва).
    """
    if max_tokens is None:
        max_tokens = settings.MAX_CONTEXT_TOKENS
    if not max_tokens or max_tokens <= 0:
        # Разумный дефолт, если лимит не задан/некорректен.
        max_tokens = 100_000
    reserved = int(max_tokens * (1.0 - _CONTEXT_RESERVE_RATIO))
    return max(reserved, _MIN_CONTEXT_LIMIT)


def estimate_tokens_fallback(text: str) -> int:
    """
    Fallback оценка количества токенов (если litellm.token_counter не работает).

    Классическая эвристика "4 символа = 1 токен" верна для английского, но СИЛЬНО
    занижает для кириллицы (там 1 символ часто = 1 токен и более из-за байтового
    BPE). Занижение приводило к тому, что обрезка контекста "думала", что всё
    помещается, а реальный запрос переполнял окно модели (ContextWindowExceededError).

    Поэтому считаем консервативно: для не-ASCII символов используем коэффициент
    ~1 токен/символ, для ASCII — ~4 символа/токен. Лучше немного переоценить и
    обрезать чуть больше, чем недооценить и упереться в окно провайдера.

    Args:
        text: Текст для оценки

    Returns:
        Примерное (консервативное) количество токенов
    """
    if not text:
        return 0
    ascii_chars = sum(1 for c in text if ord(c) < 128)
    non_ascii_chars = len(text) - ascii_chars
    # ASCII: ~4 символа/токен; не-ASCII (кириллица и пр.): ~1 токен/символ.
    return ascii_chars // 4 + non_ascii_chars + 1


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
        # Fallback: собираем весь текст и оцениваем токены консервативно
        # (estimate_tokens_fallback учитывает кириллицу). Изображения считаем
        # отдельной фиксированной оценкой (~765 токенов на изображение).
        total_tokens = 0
        image_tokens = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_tokens += estimate_tokens_fallback(content)
            elif isinstance(content, list):
                # Мультимодальный контент: суммируем текстовые части и изображения
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total_tokens += estimate_tokens_fallback(part.get("text", ""))
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        image_tokens += 765
        return total_tokens + image_tokens


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

    # Оставляем резерв под ответ модели: обрезаем историю до max_tokens минус
    # запас на генерацию (иначе даже влезающая история + ответ переполняют окно).
    effective_limit = _effective_context_limit(max_tokens)

    # Модель для токенизатора: предпочитаем зафиксированное model_name, иначе
    # deprecated-поле model. Реальное имя может быть неизвестно litellm — тогда
    # count_tokens автоматически уйдёт в консервативный fallback.
    token_model = getattr(chat_session, "model_name", None) or chat_session.model

    # Загружаем все сообщения сессии (отсортированы по created_at в модели)
    result = await session.execute(
        select(Message)
        .where(Message.session_id == chat_session.id)
        .order_by(Message.created_at)
    )
    messages_db = result.scalars().all()

    # Формируем историю
    history: list[dict[str, Any]] = []
    
    # Всегда добавляем system prompt первым.
    # build_system_prompt при включённом веб-поиске дополняет промпт инструкцией
    # активно использовать инструмент web_search (в рантайме, без изменения БД).
    system_prompt = build_system_prompt(chat_session.system_prompt)
    if system_prompt:
        history.append({"role": "system", "content": system_prompt})

    # Добавляем сообщения из БД
    for msg in messages_db:
        # Если есть content_parts (мультимодальный формат) — используем его
        if msg.content_parts:
            history.append({"role": msg.role, "content": msg.content_parts})
        else:
            # Иначе используем обычное текстовое поле
            history.append({"role": msg.role, "content": msg.content})

    # Проверяем, не превышаем ли лимит токенов (с учётом резерва под ответ)
    total_tokens = count_tokens(token_model, history)

    if total_tokens > effective_limit:
        logger.warning(
            f"Session {chat_session.id} exceeds token limit: {total_tokens} > "
            f"{effective_limit} (max={max_tokens}). Compressing (обрезаем старые сообщения)..."
        )
        history = await compress_history(
            session=session,
            chat_session=chat_session,
            history=history,
            max_tokens=effective_limit,
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
    # Модель для токенизатора (см. load_session_history): предпочитаем model_name.
    token_model = getattr(chat_session, "model_name", None) or chat_session.model

    # Сохраняем system prompt
    system_prompt = None
    if history and history[0]["role"] == "system":
        system_prompt = history[0]
        history = history[1:]

    # Удаляем старые сообщения, пока не уложимся в лимит.
    # ВАЖНО: всегда сохраняем хотя бы ПОСЛЕДНЕЕ сообщение (текущий запрос
    # пользователя) — его нельзя выкинуть, даже если оно одно превышает лимит
    # (в этом случае переполнение обработает вызывающий код дружелюбной ошибкой).
    while len(history) > 1:
        current_tokens = count_tokens(
            token_model,
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

    # Эффективный лимит с резервом под ответ модели.
    effective_limit = _effective_context_limit(max_tokens)
    token_model = getattr(chat_session, "model_name", None) or chat_session.model

    # Загружаем текущую историю (уже обрезанную под effective_limit)
    history = await load_session_history(session, chat_session, max_tokens)

    # Добавляем новое сообщение
    test_history = history + [{"role": "user", "content": new_message_content}]

    # Считаем токены
    total_tokens = count_tokens(token_model, test_history)

    if total_tokens <= effective_limit:
        return True

    logger.warning(
        f"New message would exceed limit: {total_tokens} > {effective_limit}. Attempting compression..."
    )

    # Пробуем сжать: резервируем место под новое сообщение
    new_msg_tokens = count_tokens(
        token_model, [{"role": "user", "content": new_message_content}]
    )
    compressed = await compress_history(
        session=session,
        chat_session=chat_session,
        history=history,
        max_tokens=max(effective_limit - new_msg_tokens, _MIN_CONTEXT_LIMIT),
    )

    # Проверяем снова
    final_history = compressed + [{"role": "user", "content": new_message_content}]
    final_tokens = count_tokens(token_model, final_history)

    if final_tokens <= effective_limit:
        logger.info(f"Context compressed successfully: {total_tokens} -> {final_tokens} tokens")
        return True
    else:
        logger.error(
            f"Cannot fit message even after compression: {final_tokens} > {effective_limit}"
        )
        return False
