"""
Хендлер чата со streaming для работы в топиках форума.
Поддержка текста, изображений (photo + document-картинки) и документов (PDF, текстовые файлы).
Модель и эндпоинт фиксируются за сессией при первом сообщении.
Агрегация входящих сообщений (буферизация с дебаунсом) для обработки батчей.
"""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import async_session_factory
from app.database.models import ChatSession, User
from app.keyboards.inline import (
    session_endpoint_fetch_failed_keyboard,
    session_endpoints_keyboard,
    session_favorite_models_keyboard,
    session_model_digest,
    session_model_menu_keyboard,
    session_models_fetch_failed_keyboard,
    session_models_list_keyboard,
    session_rebind_models_keyboard,
)
from app.services.attachment_service import (
    build_text_part,
    process_document,
    process_photo,
)
from app.services.context_service import (
    add_message_to_session,
    ensure_context_fits,
    load_session_history,
)
from app.services.endpoint_service import (
    get_favorite_models,
    get_models_for_owned_endpoint,
    get_owned_endpoint,
    get_user_endpoints,
)
from app.services.chat_session_service import (
    OwnedChatSession,
    get_owned_chat_session,
    get_owned_chat_session_by_topic,
    rebind_owned_chat_session,
    update_owned_chat_session_model,
)
from app.services.llm_service import LLMService

try:
    # Специфичные исключения litellm для дружелюбной обработки ошибок.
    from litellm.exceptions import ContextWindowExceededError
except Exception:  # pragma: no cover - на случай изменения структуры litellm
    ContextWindowExceededError = None  # type: ignore[assignment, misc]
from app.services.message_aggregator import get_aggregator
from app.services.user_service import get_user
from app.services.web_search import is_web_search_enabled
from app.utils.crypto import decrypt
from app.utils.formatting import escape_html, format_for_telegram, sanitize_for_streaming, split_html_for_telegram, split_plain_text
from app.utils.typing import TypingIndicator

logger = logging.getLogger(__name__)

router = Router()

# Маркеры переполнения контекста в тексте ошибки (для случаев, когда litellm
# оборачивает ContextWindowExceededError в BadRequestError и класс не совпадает).
_CONTEXT_WINDOW_ERROR_MARKERS = (
    "contextwindowexceedederror",
    "context window",
    "input is too long",
    "too long for requested model",
    "maximum context length",
    "prompt is too long",
)


def _is_context_window_error(error: Exception) -> bool:
    """
    Определяет, связана ли ошибка с переполнением контекстного окна модели.

    Проверяет как класс исключения (litellm ContextWindowExceededError), так и
    текст (на случай, когда исключение обёрнуто в BadRequestError — как в
    реальных логах, где вся цепочка fallback'ов приходит одной строкой).

    Args:
        error: Пойманное исключение.

    Returns:
        True, если ошибка про переполнение контекста.
    """
    if ContextWindowExceededError is not None and isinstance(
        error, ContextWindowExceededError
    ):
        return True
    text = str(error).lower()
    return any(marker in text for marker in _CONTEXT_WINDOW_ERROR_MARKERS)


async def get_or_create_session(
    session: AsyncSession,
    user: User,
    chat_id: int,
    thread_id: int,
    active_endpoint_id: int | None = None,
    active_model: str | None = None,
) -> tuple[ChatSession, bool]:
    """
    Получает или создаёт chat сессию для топика.
    При создании фиксирует модель и эндпоинт за сессией.
    
    Поиск сессии по (chat_id, message_thread_id) — соответствует уникальному
    ограничению uq_chat_thread. Обработка IntegrityError защищает от гонок.
    
    Args:
        session: DB сессия
        user: Пользователь
        chat_id: ID чата Telegram
        thread_id: ID топика форума
        active_endpoint_id: Активный эндпоинт пользователя (для фиксации при создании)
        active_model: Активная модель пользователя (для фиксации при создании)
        
    Returns:
        Кортеж (ChatSession, is_new): Сессия чата и флаг новой сессии
    """
    # Ищем существующую сессию по ключу уникальности: (chat_id, message_thread_id)
    # НЕ фильтруем по user_id, т.к. уникальное ограничение не включает его
    result = await session.execute(
        select(ChatSession).where(
            ChatSession.chat_id == chat_id,
            ChatSession.message_thread_id == thread_id,
        )
    )
    chat_session = result.scalar_one_or_none()

    if not chat_session:
        # Создаём новую сессию с моделью пользователя
        model = active_model or user.active_model or "gpt-3.5-turbo"  # fallback

        chat_session = ChatSession(
            user_id=user.telegram_id,
            chat_id=chat_id,
            message_thread_id=thread_id,
            model=model,  # Deprecated поле (для обратной совместимости)
            model_name=model,  # Новое поле: зафиксированная модель
            endpoint_id=active_endpoint_id or user.active_endpoint_id,  # Фиксируем эндпоинт
            system_prompt="You are a helpful assistant.",
        )
        session.add(chat_session)
        
        try:
            await session.flush()
            logger.info(
                f"Создана новая сессия чата для пользователя {user.telegram_id}, "
                f"топик {thread_id}, модель: {model}, endpoint_id: {chat_session.endpoint_id}"
            )
            return chat_session, True
        except IntegrityError:
            # Конфликт вставки — сессия была создана параллельным запросом
            await session.rollback()
            logger.info(
                f"Конфликт вставки сессии для топика {thread_id}, "
                f"загружаем существующую"
            )
            
            # Повторный SELECT существующей сессии
            result = await session.execute(
                select(ChatSession).where(
                    ChatSession.chat_id == chat_id,
                    ChatSession.message_thread_id == thread_id,
                )
            )
            chat_session = result.scalar_one()
            return chat_session, False

    return chat_session, False


async def process_message_batch(batch: list[Message], bot: Bot) -> None:
    """
    Обработка батча сообщений: склейка контента, вызов LLM, отправка ответа.
    
    Эта функция вызывается агрегатором после дебаунса.
    Создаёт свою БД-сессию (не использует middleware session).
    
    Args:
        batch: Список сообщений для обработки (отсортированы по времени)
        bot: Экземпляр aiogram.Bot
    """
    if not batch:
        return
    
    # Берём данные из первого сообщения батча
    first_message = batch[0]
    user_id = first_message.from_user.id if first_message.from_user else None
    chat_id = first_message.chat.id
    thread_id = first_message.message_thread_id
    
    if not user_id:
        logger.warning("Батч сообщений без from_user, пропускаем")
        return
    
    # Создаём новую БД-сессию для отложенной обработки
    async with async_session_factory() as session:
        try:
            # 1. Получаем пользователя
            user = await get_user(session, user_id)
            
            if not user:
                await first_message.reply(
                    "⚠️ Вы не зарегистрированы. Используйте /start в личке с ботом."
                )
                return

            # 2. Получаем/создаём сессию чата
            chat_session, is_new_session = await get_or_create_session(
                session, user, chat_id, thread_id,
                active_endpoint_id=user.active_endpoint_id,
                active_model=user.active_model,
            )
            
            # Уникальность сессии задана координатами топика, поэтому существующая
            # запись может принадлежать только первоначальному владельцу сессии.
            # Другой участник группового топика не должен читать/изменять её.
            if chat_session.user_id != user.telegram_id:
                logger.warning(
                    "Пользователь %s попытался использовать сессию %s владельца %s",
                    user.telegram_id,
                    chat_session.id,
                    chat_session.user_id,
                )
                await first_message.reply(
                    "❌ Сессия этого топика принадлежит другому пользователю. "
                    "Доступ к её модели и истории запрещён."
                )
                return

            # 3. Определяем модель и эндпоинт для использования
            if chat_session.model_name:
                session_model = chat_session.model_name
                session_endpoint_id = chat_session.endpoint_id
            else:
                # Первое сообщение в существующей сессии (миграция со старой схемы)
                if not user.active_endpoint_id or not user.active_model:
                    await first_message.reply(
                        "⚠️ Сначала настройте эндпоинт и модель через /settings в личке с ботом."
                    )
                    return
                
                session_model = user.active_model
                session_endpoint_id = user.active_endpoint_id
                
                # Фиксируем за сессией оба поля модели для обратной совместимости.
                chat_session.model_name = session_model
                chat_session.model = session_model
                chat_session.endpoint_id = session_endpoint_id
                await session.flush()
                is_new_session = True
                
                logger.info(
                    f"Зафиксирована модель {session_model} за существующей сессией {chat_session.id}"
                )
            
            # 4. Получаем эндпоинт
            if session_endpoint_id:
                endpoint = await get_owned_endpoint(
                    session, user.telegram_id, session_endpoint_id
                )
                if not endpoint:
                    logger.warning(
                        "Зафиксированный endpoint %s сессии %s удалён или не принадлежит владельцу",
                        session_endpoint_id,
                        chat_session.id,
                    )
                    await first_message.reply(
                        "❌ Endpoint этого топика удалён или недоступен. История сохранена. "
                        "Используйте /model в этом топике, чтобы выбрать новый endpoint "
                        "и совместимую модель."
                    )
                    return
            else:
                # NULL означает удалённую/непривязанную пару сессии. Глобально
                # активный endpoint показывается в /model только как вариант и не
                # должен применяться неявно: модель принадлежит прежнему endpoint.
                await first_message.reply(
                    "❌ Endpoint этого топика удалён или не выбран. История сохранена. "
                    "Используйте /model в этом топике, чтобы явно выбрать новый "
                    "endpoint и совместимую модель."
                )
                return

            api_key = decrypt(endpoint.api_key_encrypted)

            # 5. Закрепляем инфо-сообщение о модели (только для новой сессии)
            if is_new_session and not chat_session.pinned_message_id:
                try:
                    model_display_name = escape_html(session_model)
                    info_text = f"🤖 Модель этого чата: <code>{model_display_name}</code>"
                    
                    info_msg = await bot.send_message(
                        chat_id=chat_id,
                        text=info_text,
                        parse_mode="HTML",
                        message_thread_id=thread_id,
                    )
                    
                    try:
                        await bot.pin_chat_message(
                            chat_id=chat_id,
                            message_id=info_msg.message_id,
                            disable_notification=True,
                        )
                        chat_session.pinned_message_id = info_msg.message_id
                        await session.flush()
                        logger.info(
                            f"Закреплено инфо-сообщение о модели для сессии {chat_session.id}, "
                            f"message_id={info_msg.message_id}"
                        )
                    except (TelegramBadRequest, TelegramForbiddenError) as pin_error:
                        logger.warning(
                            f"Не удалось закрепить инфо-сообщение для сессии {chat_session.id}: "
                            f"{pin_error}. Сообщение отправлено, но не закреплено."
                        )
                        chat_session.pinned_message_id = info_msg.message_id
                        await session.flush()
                except Exception as info_error:
                    logger.error(
                        f"Не удалось отправить инфо-сообщение о модели для сессии "
                        f"{chat_session.id}: {info_error}",
                        exc_info=True,
                    )
            
            # 6. Автопереименование топика по первому сообщению батча
            if not chat_session.topic_renamed and first_message.message_thread_id:
                try:
                    # Формируем краткое название из текста первого сообщения батча
                    user_text = first_message.text or first_message.caption or ""
                    
                    if not user_text.strip():
                        # Нет текста — определяем тип контента
                        if first_message.photo:
                            topic_name = "🖼 Изображение"
                        elif first_message.document:
                            topic_name = f"📄 {first_message.document.file_name or 'Документ'}"
                        else:
                            topic_name = "💬 Диалог"
                    else:
                        # Обрезаем текст для названия топика (Telegram лимит: 1-128 символов)
                        max_length = 50
                        topic_name = user_text.replace("\n", " ").strip()
                        if len(topic_name) > max_length:
                            topic_name = topic_name[:max_length].rstrip() + "…"
                    
                    await bot.edit_forum_topic(
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        name=topic_name,
                    )
                    
                    chat_session.topic_renamed = True
                    await session.flush()
                    
                    logger.info(
                        f"Топик {thread_id} переименован в '{topic_name}' для сессии {chat_session.id}"
                    )
                except (TelegramBadRequest, TelegramForbiddenError) as rename_error:
                    logger.warning(
                        f"Не удалось переименовать топик {thread_id} для сессии {chat_session.id}: "
                        f"{rename_error}. Продолжаем без переименования."
                    )
                    chat_session.topic_renamed = True
                    await session.flush()
                except Exception as rename_error:
                    logger.error(
                        f"Ошибка при переименовании топика {thread_id}: {rename_error}",
                        exc_info=True,
                    )
                    chat_session.topic_renamed = True
                    await session.flush()
            
            # 7. Собираем content_parts из ВСЕХ сообщений батча
            all_content_parts: list[dict] = []
            
            # Обрабатываем каждое сообщение батча по порядку
            for msg in batch:
                # Текст (из text или caption)
                text = msg.text or msg.caption or ""
                if text.strip():
                    all_content_parts.append(build_text_part(text))
                
                # Фото (берём наибольшее)
                if msg.photo:
                    try:
                        photo = msg.photo[-1]
                        logger.info(f"Обработка фото из батча: file_id={photo.file_id}")
                        image_part = await process_photo(bot, photo.file_id, "image/jpeg")
                        all_content_parts.append(image_part)
                    except Exception as e:
                        logger.error(f"Ошибка обработки фото из батча: {e}", exc_info=True)
                        await first_message.reply(
                            f"❌ Не удалось обработать изображение: {str(e)}"
                        )
                        return
                
                # Документ
                if msg.document:
                    try:
                        doc = msg.document
                        logger.info(
                            f"Обработка документа из батча: {doc.file_name}, "
                            f"mime_type={doc.mime_type}"
                        )
                        doc_part = await process_document(
                            bot,
                            doc.file_id,
                            doc.file_name,
                            doc.mime_type,
                        )
                        all_content_parts.append(doc_part)
                    except Exception as e:
                        logger.error(f"Ошибка обработки документа из батча: {e}", exc_info=True)
                        await first_message.reply(
                            f"❌ Не удалось обработать документ: {str(e)}"
                        )
                        return
            
            # Если ничего не распознано — игнорируем
            if not all_content_parts:
                logger.debug(
                    f"Пустой батч от пользователя {user_id}, пропускаем"
                )
                return
            
            # 8. Определяем тип контента
            # Если только один text-part → сохраняем как обычный текст
            # Иначе → мультимодальный формат
            is_multimodal = len(all_content_parts) > 1 or (
                len(all_content_parts) == 1 and all_content_parts[0].get("type") != "text"
            )
            
            # Текстовая выжимка для проверки контекста и сохранения
            text_summary = " ".join(
                part.get("text", "")
                for part in all_content_parts
                if part.get("type") == "text"
            )
            if not text_summary.strip():
                text_summary = "[мультимодальное сообщение без текста]"

            # 9. Проверяем, что новое сообщение поместится в контекст
            if not await ensure_context_fits(session, chat_session, text_summary):
                await first_message.reply(
                    "❌ Сообщение слишком длинное и не помещается в контекст даже "
                    "после сжатия. Попробуйте сократить запрос или начните новый топик."
                )
                return

            # 10. Сохраняем объединённое сообщение пользователя
            await add_message_to_session(
                session,
                chat_session,
                "user",
                text_summary,
                content_parts=all_content_parts if is_multimodal else None,
            )
            await session.commit()

            # 11. Загружаем полную историю (с callback для статуса сжатия)
            # Отправляем начальное сообщение для потенциального статуса сжатия
            status_msg = await first_message.reply("💭 Подготовка...")
            
            async def compression_status_callback(status: str) -> None:
                """Показывает статус сжатия контекста пользователю."""
                try:
                    await status_msg.edit_text(status)
                except Exception as e:
                    logger.warning(f"Не удалось обновить статус сжатия: {e}")
            
            history = await load_session_history(
                session, chat_session, compression_callback=compression_status_callback
            )

            # 12. Создаём LLM service
            llm = LLMService(
                base_url=endpoint.base_url,
                api_key=api_key,
                timeout=120,
            )

            # 13. Streaming генерация с typing-индикатором
            accumulated_text = ""
            # Отслеживаем последний фактически отправленный в Telegram текст,
            # чтобы не делать бессмысленный edit (который приводит к
            # "message is not modified") ни во время стрима, ни на финальном шаге.
            last_sent_text: str | None = None
            last_update_time = asyncio.get_event_loop().time()
            update_interval = 1.5  # секунды между обновлениями сообщения
            # Безопасный лимит для предпросмотра во время стриминга (резерв под возможные символы)
            STREAMING_PREVIEW_LIMIT = 3800

            # Используем существующее сообщение для ответа (переписываем статус)
            try:
                await status_msg.edit_text("💭 Думаю...")
            except Exception:
                # Если не удалось отредактировать — отправляем новое
                status_msg = await first_message.reply("💭 Думаю...")
            reply_msg = status_msg

            # Режим работы: если веб-поиск включён — агентный СТРИМИНГ с tools
            # (финальный ответ стримится, на этапе поиска показывается статус),
            # иначе — обычный стриминг. В обоих случаях финальный ответ обновляется
            # динамически через один и тот же цикл потребления токенов.
            web_search_active = is_web_search_enabled()
            if web_search_active:
                logger.info(
                    f"Using agentic_stream (web search enabled) для пользователя "
                    f"{user_id}, топик {thread_id}, модель {session_model}"
                )
            else:
                logger.info(
                    f"Using streaming (no web search) для пользователя {user_id}, "
                    f"топик {thread_id}, модель {session_model}"
                )

            # Флаг: после статуса поиска нужно немедленно показать первый токен
            # финального ответа (сбросить троттлинг), чтобы «🔎 Ищу…» сразу сменился.
            force_next_update = False

            async def on_search(query: str) -> None:
                nonlocal force_next_update
                # Обрезаем длинный запрос для отображения
                display_query = query if len(query) <= 60 else query[:60].rstrip() + "…"
                status_text = f"🔎 Ищу в интернете: {display_query}…"
                try:
                    await reply_msg.edit_text(status_text)
                except TelegramRetryAfter as e:
                    logger.warning(
                        f"Rate limit при обновлении статуса поиска: retry_after={e.retry_after}s"
                    )
                    await asyncio.sleep(e.retry_after)
                except Exception as e:
                    logger.warning(f"Не удалось обновить статус поиска: {e}")
                # После поиска первый пришедший токен должен сразу заменить статус
                force_next_update = True

            try:
                # Выбираем источник токенов: агентный стрим (с поиском) или обычный стрим
                if web_search_active:
                    token_source = llm.agentic_stream(
                        model=session_model,
                        messages=history,
                        on_search=on_search,
                    )
                else:
                    token_source = llm.stream_chat_completion(
                        model=session_model,
                        messages=history,
                    )

                # Единый цикл потребления токенов с троттлингом и предпросмотром.
                async with TypingIndicator(bot, chat_id, thread_id):
                    async for token in token_source:
                        accumulated_text += token

                        # Throttled update: обновляем раз в update_interval сек,
                        # либо немедленно после завершения поиска (force_next_update).
                        current_time = asyncio.get_event_loop().time()
                        if force_next_update or (current_time - last_update_time >= update_interval):
                            try:
                                # Во время streaming показываем ПРЕДПРОСМОТР (последние N символов)
                                # чтобы не превысить лимит Telegram 4096. Plain text без parse_mode
                                # для избежания ошибок невалидного HTML на неполном потоке.
                                preview_text = accumulated_text[-STREAMING_PREVIEW_LIMIT:] if len(accumulated_text) > STREAMING_PREVIEW_LIMIT else accumulated_text
                                preview_text = sanitize_for_streaming(preview_text)

                                # Пропускаем edit, если текст не изменился с прошлого раза —
                                # иначе Telegram вернёт "message is not modified".
                                if preview_text != last_sent_text:
                                    await reply_msg.edit_text(preview_text)
                                    last_sent_text = preview_text
                                last_update_time = current_time
                                force_next_update = False
                            except TelegramRetryAfter as e:
                                # Rate limit от Telegram — ждём и продолжаем
                                logger.warning(
                                    f"Rate limit при streaming edit: retry_after={e.retry_after}s"
                                )
                                await asyncio.sleep(e.retry_after)
                            except TelegramBadRequest as e:
                                # "message is not modified" — не ошибка: текст уже актуален.
                                if "message is not modified" in str(e).lower():
                                    last_sent_text = preview_text
                                    last_update_time = current_time
                                    force_next_update = False
                                else:
                                    logger.warning(
                                        f"Не удалось отредактировать сообщение при streaming: {e}"
                                    )
                            except Exception as e:
                                # Другие ошибки редактирования — логируем и продолжаем
                                logger.warning(
                                    f"Не удалось отредактировать сообщение при streaming: {e}"
                                )

            except asyncio.TimeoutError:
                await reply_msg.edit_text("❌ Timeout: модель не ответила вовремя.")
                logger.error(
                    f"Timeout при генерации для пользователя {user_id}, топик {thread_id}"
                )
                return
            except Exception as e:
                # Дружелюбная обработка переполнения контекста: даже после обрезки
                # истории модель (или её fallback с меньшим окном) может вернуть
                # ContextWindowExceededError. Показываем понятное сообщение вместо
                # сырого стектрейса litellm.
                if _is_context_window_error(e):
                    logger.warning(
                        f"Контекст переполнен для пользователя {user_id}, топик {thread_id}: {e}"
                    )
                    await reply_msg.edit_text(
                        "❌ Контекст диалога слишком большой и не помещается в модель. "
                        "Начните новый топик/сессию или сократите запрос, чтобы продолжить."
                    )
                    return
                logger.error(f"Ошибка LLM streaming: {e}", exc_info=True)
                error_text = str(e).replace("&", "&").replace("<", "<").replace(">", ">")
                await reply_msg.edit_text(f"❌ Ошибка генерации: {error_text}")
                return

            # 14. Финальное обновление с полным HTML-форматированием и нарезкой
            if accumulated_text.strip():
                formatted_text = format_for_telegram(accumulated_text)
                
                # Нарезаем на части (если превышает лимит)
                parts = split_html_for_telegram(formatted_text, limit=4096)
                
                if not parts:
                    logger.warning("split_html_for_telegram вернул пустой список")
                    await reply_msg.edit_text("⚠️ Модель вернула пустой ответ")
                    return
                
                # Отправка частей
                async def send_part_with_retry(
                    text: str,
                    is_edit: bool = False,
                    target_msg = None,
                ) -> bool:
                    """
                    Отправляет/редактирует часть с retry при TelegramRetryAfter
                    и fallback на plain text (с нарезкой!) при TelegramBadRequest.
                    
                    Returns:
                        True если успешно, False если не удалось
                    """
                    nonlocal last_sent_text
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            if is_edit and target_msg:
                                await target_msg.edit_text(text, parse_mode="HTML")
                            else:
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=text,
                                    parse_mode="HTML",
                                    message_thread_id=thread_id,
                                )
                            return True
                        except TelegramRetryAfter as e:
                            logger.warning(
                                f"Rate limit при отправке части: retry_after={e.retry_after}s, "
                                f"попытка {attempt + 1}/{max_retries}"
                            )
                            await asyncio.sleep(e.retry_after)
                        except TelegramBadRequest as e:
                            # "message is not modified" — не ошибка: текущий текст сообщения
                            # уже идентичен целевому. Это НЕ повод слать новое сообщение
                            # (иначе получаем дубликат) и НЕ повод делать fallback.
                            if "message is not modified" in str(e).lower():
                                logger.debug(
                                    "Финальный edit пропущен: message is not modified "
                                    "(текст уже актуален)."
                                )
                                return True
                            logger.error(
                                f"Невалидный HTML в финальном сообщении: {e}. Отправка без форматирования."
                            )
                            # Fallback: нарезаем plain text и отправляем части
                            try:
                                plain_parts = split_plain_text(text, limit=4096)
                                
                                if not plain_parts:
                                    logger.error("split_plain_text вернул пустой список")
                                    return False
                                
                                # Первая часть — редактируем/отправляем
                                if is_edit and target_msg:
                                    try:
                                        await target_msg.edit_text(plain_parts[0])
                                    except TelegramBadRequest as edit_err:
                                        # "message is not modified" — текст уже актуален:
                                        # НЕ шлём новое сообщение (иначе дубликат), считаем успехом.
                                        if "message is not modified" in str(edit_err).lower():
                                            logger.debug(
                                                "Fallback edit пропущен: message is not modified "
                                                "(текст уже актуален)."
                                            )
                                        else:
                                            # Реальная ошибка edit — отправляем новым сообщением
                                            logger.warning(f"Fallback edit тоже не удался: {edit_err}. Отправка новым сообщением.")
                                            await bot.send_message(
                                                chat_id=chat_id,
                                                text=plain_parts[0],
                                                message_thread_id=thread_id,
                                            )
                                else:
                                    await bot.send_message(
                                        chat_id=chat_id,
                                        text=plain_parts[0],
                                        message_thread_id=thread_id,
                                    )
                                
                                # Остальные части — отправляем новыми сообщениями
                                for plain_part in plain_parts[1:]:
                                    await asyncio.sleep(0.3)  # Анти-флуд
                                    await bot.send_message(
                                        chat_id=chat_id,
                                        text=plain_part,
                                        message_thread_id=thread_id,
                                    )
                                
                                return True
                            except Exception as fallback_error:
                                logger.error(
                                    f"Fallback plain text с нарезкой не удался: {fallback_error}"
                                )
                                return False
                        except Exception as e:
                            logger.error(
                                f"Ошибка отправки части (попытка {attempt + 1}/{max_retries}): {e}"
                            )
                            if attempt < max_retries - 1:
                                await asyncio.sleep(1.0)
                            else:
                                return False
                    
                    return False
                
                # Если одна часть — просто редактируем существующее сообщение
                if len(parts) == 1:
                    # Пропускаем бессмысленный финальный edit: если HTML-форматированный
                    # текст идентичен последнему отправленному во время стрима preview —
                    # редактировать нечего (Telegram вернул бы "message is not modified").
                    if parts[0] == last_sent_text:
                        logger.debug(
                            "Финальный edit пропущен: текст идентичен последнему "
                            "отправленному во время стриминга."
                        )
                        success = True
                    else:
                        success = await send_part_with_retry(
                            parts[0],
                            is_edit=True,
                            target_msg=reply_msg,
                        )
                    if not success:
                        logger.error("Не удалось отправить единственную часть ответа")
                else:
                    # Несколько частей: редактируем первое сообщение, остальные отправляем новыми
                    logger.info(
                        f"Ответ разбит на {len(parts)} частей (лимит 4096 символов)"
                    )
                    
                    # Первая часть — редактируем существующее сообщение
                    success = await send_part_with_retry(
                        parts[0],
                        is_edit=True,
                        target_msg=reply_msg,
                    )
                    
                    if not success:
                        logger.error("Не удалось отредактировать первую часть ответа")
                    
                    # Остальные части — отправляем новыми сообщениями с анти-флуд задержкой
                    for idx, part in enumerate(parts[1:], start=2):
                        # Анти-флуд задержка между частями
                        await asyncio.sleep(0.3)
                        
                        success = await send_part_with_retry(part, is_edit=False)
                        
                        if not success:
                            logger.error(
                                f"Не удалось отправить часть {idx}/{len(parts)} ответа"
                            )
                            # Пытаемся уведомить пользователя об ошибке
                            try:
                                await bot.send_message(
                                    chat_id=chat_id,
                                    text=f"⚠️ Часть {idx}/{len(parts)} не удалось отправить",
                                    message_thread_id=thread_id,
                                )
                            except Exception:
                                pass
            else:
                logger.warning("Пустой ответ от LLM")
                await reply_msg.edit_text("⚠️ Модель вернула пустой ответ")
                return

            # 15. Сохраняем ответ ассистента
            await add_message_to_session(
                session,
                chat_session,
                "assistant",
                accumulated_text,
            )
            await session.commit()

            logger.info(
                f"Успешно сгенерирован ответ для пользователя {user_id}, "
                f"топик {thread_id}, батч из {len(batch)} сообщений, "
                f"{len(accumulated_text)} символов"
            )

        except Exception as e:
            # Переполнение контекста может прилететь и вне streaming-блока —
            # показываем дружелюбное сообщение вместо сырого стектрейса.
            if _is_context_window_error(e):
                logger.warning(
                    f"Контекст переполнен (обработка батча) для пользователя "
                    f"{user_id}, топик {thread_id}: {e}"
                )
                try:
                    await first_message.reply(
                        "❌ Контекст диалога слишком большой и не помещается в модель. "
                        "Начните новый топик/сессию или сократите запрос, чтобы продолжить."
                    )
                except Exception as reply_error:
                    logger.error(f"Не удалось отправить сообщение об ошибке: {reply_error}")
                return

            logger.error(
                f"Непредвиденная ошибка при обработке батча сообщений: {e}",
                exc_info=True,
            )
            error_text = str(e).replace("&", "&").replace("<", "<").replace(">", ">")
            try:
                await first_message.reply(f"❌ Непредвиденная ошибка: {error_text}")
            except Exception as reply_error:
                logger.error(f"Не удалось отправить сообщение об ошибке: {reply_error}")


def _session_model_text(owned: OwnedChatSession, *, section: str = "") -> str:
    """Формирует HTML-карточку модели сессии без чувствительных данных endpoint."""
    if owned.endpoint is None:
        raise ValueError("Endpoint сессии удалён или недоступен")
    current_model = owned.chat_session.model_name or owned.chat_session.model
    suffix = f"\n\n{section}" if section else ""
    return (
        "🤖 <b>Модель текущего топика</b>\n\n"
        f"<b>Endpoint:</b> {escape_html(owned.endpoint.name)}\n"
        f"<b>Текущая модель:</b> <code>{escape_html(current_model)}</code>"
        f"{suffix}"
    )


async def _load_callback_session(
    callback: CallbackQuery,
    session: AsyncSession,
    session_id: int,
    *,
    allow_missing_endpoint: bool = False,
) -> OwnedChatSession | None:
    """Строго связывает callback с владельцем, чатом и forum topic сообщения."""
    message = callback.message
    chat = getattr(message, "chat", None)
    thread_id = getattr(message, "message_thread_id", None)
    if message is None or chat is None or thread_id is None:
        await callback.answer("❌ Меню недоступно вне топика", show_alert=True)
        return None

    owned = await get_owned_chat_session(
        session,
        session_id=session_id,
        user_id=callback.from_user.id,
        chat_id=chat.id,
        message_thread_id=thread_id,
    )
    if owned is None:
        await callback.answer("❌ Сессия не найдена или доступ запрещён", show_alert=True)
        return None
    if owned.endpoint is None and not allow_missing_endpoint:
        text, keyboard = await _session_endpoints_view(session, owned)
        try:
            await callback.message.edit_text(
                text, reply_markup=keyboard, parse_mode="HTML"
            )
        except Exception as error:
            logger.warning(
                "Endpoint сессии %s исчез, recovery UI не обновлён: %s",
                session_id,
                type(error).__name__,
            )
        await callback.answer(
            "❌ Endpoint удалён. История сохранена; выберите другой endpoint.",
            show_alert=True,
        )
        return None
    return owned


async def _available_session_models(
    session: AsyncSession, owned: OwnedChatSession
) -> list[str]:
    """Заново получает модели owner-scoped endpoint и убирает дубликаты."""
    if owned.endpoint is None:
        raise ValueError("Endpoint сессии удалён или недоступен")
    models = await get_models_for_owned_endpoint(
        session,
        owned.chat_session.user_id,
        owned.endpoint.id,
    )
    return list(dict.fromkeys(models))


def _parse_callback_id(raw: str) -> int:
    """Разбирает положительный lowercase base36 ID из callback_data."""
    if not raw or any(char not in "0123456789abcdefghijklmnopqrstuvwxyz" for char in raw):
        raise ValueError
    value = int(raw, 36)
    if value < 1:
        raise ValueError
    return value


def _parse_expected_endpoint(raw: str) -> int | None:
    """Разбирает base36 CAS-токен старого endpoint из callback."""
    return None if raw == "n" else _parse_callback_id(raw)


async def _session_endpoints_view(
    session: AsyncSession,
    owned: OwnedChatSession,
) -> tuple[str, object]:
    """Строит recovery/смену endpoint только из endpoint-ов владельца."""
    endpoints = await get_user_endpoints(session, owned.chat_session.user_id)
    user = await get_user(session, owned.chat_session.user_id)
    current_model = owned.chat_session.model_name or owned.chat_session.model
    if owned.endpoint is None:
        heading = (
            "❌ <b>Endpoint этой сессии удалён</b>\n\n"
            "История диалога и прежнее имя модели сохранены."
        )
    else:
        heading = (
            "🔌 <b>Смена endpoint текущего топика</b>\n\n"
            f"Текущий endpoint: <b>{escape_html(owned.endpoint.name)}</b>"
        )
    # В явном flow смены текущий endpoint не предлагаем как "новый": это делает
    # CAS одноразовым и не позволяет повторному callback повторить запись.
    selectable_endpoints = (
        endpoints
        if owned.chat_session.endpoint_id is None
        else [
            endpoint for endpoint in endpoints
            if endpoint.id != owned.chat_session.endpoint_id
        ]
    )
    if selectable_endpoints:
        text = (
            f"{heading}\n"
            f"Текущая модель: <code>{escape_html(current_model)}</code>\n\n"
            "Выберите endpoint. ✅ отмечен глобально активный endpoint. "
            "Глобальные настройки и история изменены не будут."
        )
    else:
        text = (
            f"{heading}\n"
            f"Текущая модель: <code>{escape_html(current_model)}</code>\n\n"
            + (
                "У вас нет доступных endpoint. Добавьте endpoint в личном чате с ботом: "
                "/settings → «Мои эндпоинты» → «Добавить эндпоинт»."
                if owned.chat_session.endpoint_id is None
                else "Других доступных endpoint у вас нет. Добавить endpoint можно в "
                "личном чате: /settings → «Мои эндпоинты» → «Добавить эндпоинт»."
            )
        )
    keyboard = session_endpoints_keyboard(
        owned.chat_session.id,
        owned.chat_session.endpoint_id,
        selectable_endpoints,
        user.active_endpoint_id if user else None,
    )
    return text, keyboard


async def _show_session_favorites(
    callback: CallbackQuery,
    session: AsyncSession,
    owned: OwnedChatSession,
) -> None:
    models = await _available_session_models(session, owned)
    available = set(models)
    favorites = await get_favorite_models(session, callback.from_user.id)
    favorite_names = [
        favorite.model_name
        for favorite in favorites
        if favorite.endpoint_id == owned.endpoint.id
        and favorite.model_name in available
    ]
    current_model = owned.chat_session.model_name or owned.chat_session.model
    section = (
        "⭐ <b>Избранные модели этого endpoint:</b>"
        if favorite_names
        else "⭐ Избранных доступных моделей для этого endpoint нет."
    )
    await callback.message.edit_text(
        _session_model_text(owned, section=section),
        reply_markup=session_favorite_models_keyboard(
            owned.chat_session.id,
            favorite_names,
            current_model,
        ),
        parse_mode="HTML",
    )


@router.message(
    Command("model"),
    F.chat.type.in_({"supergroup", "group"}),
    F.message_thread_id,
)
async def cmd_session_model(message: Message, session: AsyncSession) -> None:
    """Показывает выбор модели только для уже существующей сессии forum topic."""
    if message.from_user is None or message.message_thread_id is None:
        return
    owned = await get_owned_chat_session_by_topic(
        session,
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        message_thread_id=message.message_thread_id,
    )
    if owned is None:
        await message.answer(
            "❌ Для этого топика нет доступной сессии. Сначала отправьте обычное сообщение."
        )
        return
    if owned.endpoint is None:
        text, keyboard = await _session_endpoints_view(session, owned)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        return

    try:
        models = await _available_session_models(session, owned)
        available = set(models)
        favorites = await get_favorite_models(session, message.from_user.id)
        favorite_names = [
            favorite.model_name
            for favorite in favorites
            if favorite.endpoint_id == owned.endpoint.id
            and favorite.model_name in available
        ]
    except Exception as error:
        logger.warning(
            "Не удалось получить модели endpoint %s для /model: %s",
            owned.endpoint.id,
            type(error).__name__,
        )
        await message.answer(
            "⚠️ <b>Endpoint существует, но список моделей сейчас недоступен.</b>\n\n"
            "Данные сессии не изменены. Можно повторить запрос или выбрать другой endpoint.",
            reply_markup=session_models_fetch_failed_keyboard(
                owned.chat_session.id,
            ),
            parse_mode="HTML",
        )
        return

    current_model = owned.chat_session.model_name or owned.chat_session.model
    section = (
        "⭐ <b>Избранные модели этого endpoint:</b>"
        if favorite_names
        else "⭐ Избранных доступных моделей для этого endpoint нет."
    )
    await message.answer(
        _session_model_text(owned, section=section),
        reply_markup=session_favorite_models_keyboard(
            owned.chat_session.id,
            favorite_names,
            current_model,
        ),
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^se:e:[0-9]+$"))
async def callback_session_endpoints(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    """Показывает актуальные owner-scoped endpoint-ы для recovery/смены."""
    try:
        session_id = int(callback.data.split(":")[2])
    except (AttributeError, IndexError, ValueError):
        await callback.answer("❌ Некорректное меню", show_alert=True)
        return
    owned = await _load_callback_session(
        callback, session, session_id, allow_missing_endpoint=True
    )
    if owned is None:
        return
    text, keyboard = await _session_endpoints_view(session, owned)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()


@router.callback_query(F.data.regexp(r"^se:p:[0-9a-z]+:(n|[0-9a-z]+):[0-9a-z]+:[0-9]+$"))
async def callback_session_endpoint_models(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    """Получает свежие модели выбранного endpoint и показывает страницу."""
    try:
        _, _, session_raw, old_raw, endpoint_raw, page_raw = callback.data.split(":")
        session_id = _parse_callback_id(session_raw)
        expected_endpoint_id = _parse_expected_endpoint(old_raw)
        endpoint_id = _parse_callback_id(endpoint_raw)
        page = int(page_raw)
        if endpoint_id < 1 or page < 0:
            raise ValueError
    except (AttributeError, ValueError):
        await callback.answer("❌ Некорректный выбор endpoint", show_alert=True)
        return
    owned = await _load_callback_session(
        callback, session, session_id, allow_missing_endpoint=True
    )
    if owned is None:
        return
    if owned.chat_session.endpoint_id != expected_endpoint_id:
        await callback.answer("❌ Меню устарело: сессия уже изменилась", show_alert=True)
        return
    endpoint = await get_owned_endpoint(session, callback.from_user.id, endpoint_id)
    if endpoint is None:
        await callback.answer(
            "❌ Endpoint удалён или больше недоступен. Выберите актуальный.",
            show_alert=True,
        )
        text, keyboard = await _session_endpoints_view(session, owned)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        return
    try:
        models = list(dict.fromkeys(await get_models_for_owned_endpoint(
            session, callback.from_user.id, endpoint_id
        )))
    except Exception as error:
        logger.warning(
            "Не удалось получить модели endpoint %s для смены сессии %s: %s",
            endpoint_id, session_id, type(error).__name__,
        )
        await callback.message.edit_text(
            "⚠️ <b>Endpoint существует, но список моделей сейчас недоступен.</b>\n\n"
            "Сессия и история не изменены.",
            reply_markup=session_endpoint_fetch_failed_keyboard(
                session_id, expected_endpoint_id, endpoint_id
            ),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    if not models:
        await callback.message.edit_text(
            "⚠️ <b>Endpoint существует, но не вернул доступных моделей.</b>\n\n"
            "Сессия и история не изменены.",
            reply_markup=session_endpoint_fetch_failed_keyboard(
                session_id, expected_endpoint_id, endpoint_id
            ),
            parse_mode="HTML",
        )
        await callback.answer()
        return
    max_page = (len(models) - 1) // 8
    if page > max_page:
        await callback.answer("❌ Страница устарела; список обновлён", show_alert=True)
        page = 0
    favorites = await get_favorite_models(session, callback.from_user.id)
    favorite_names = {
        favorite.model_name for favorite in favorites
        if favorite.endpoint_id == endpoint_id
    }
    await callback.message.edit_text(
        "🔌 <b>Выбран новый endpoint:</b> "
        f"{escape_html(endpoint.name)}\n\n"
        "Выберите модель. Нажатие модели одновременно сменит endpoint и модель "
        "только для текущего топика; история сохранится.\n\n"
        f"📋 Страница {page + 1}/{max_page + 1}",
        reply_markup=session_rebind_models_keyboard(
            session_id, expected_endpoint_id, endpoint_id, models, favorite_names, page
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(
    F.data.regexp(r"^se:s:[0-9a-z]+:(n|[0-9a-z]+):[0-9a-z]+:[0-9a-f]{12}$")
)
async def callback_session_endpoint_model_select(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    """Разрешает digest по свежему списку и CAS-перепривязывает сессию."""
    try:
        _, _, session_raw, old_raw, endpoint_raw, digest = callback.data.split(":")
        session_id = _parse_callback_id(session_raw)
        expected_endpoint_id = _parse_expected_endpoint(old_raw)
        endpoint_id = _parse_callback_id(endpoint_raw)
        if len(digest) != 12:
            raise ValueError
    except (AttributeError, ValueError):
        await callback.answer("❌ Некорректный выбор модели", show_alert=True)
        return
    owned = await _load_callback_session(
        callback, session, session_id, allow_missing_endpoint=True
    )
    if owned is None:
        return
    if owned.chat_session.endpoint_id != expected_endpoint_id:
        await callback.answer("❌ Меню устарело: сессия уже изменилась", show_alert=True)
        return
    endpoint = await get_owned_endpoint(session, callback.from_user.id, endpoint_id)
    if endpoint is None:
        await callback.answer("❌ Endpoint исчез. Выберите другой.", show_alert=True)
        text, keyboard = await _session_endpoints_view(session, owned)
        await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
        return
    try:
        models = list(dict.fromkeys(await get_models_for_owned_endpoint(
            session, callback.from_user.id, endpoint_id
        )))
    except Exception as error:
        logger.warning(
            "Не удалось повторно получить модели endpoint %s сессии %s: %s",
            endpoint_id, session_id, type(error).__name__,
        )
        await callback.answer("❌ Список моделей недоступен; запись не изменена", show_alert=True)
        return
    matches = [model for model in models if session_model_digest(model) == digest]
    if len(matches) != 1:
        logger.warning(
            "Digest модели не разрешён однозначно при rebind сессии %s: matches=%s",
            session_id, len(matches),
        )
        await callback.answer(
            "❌ Модель исчезла или идентификатор неоднозначен. Обновите список.",
            show_alert=True,
        )
        return
    new_model = matches[0]
    updated = await rebind_owned_chat_session(
        session,
        session_id=session_id,
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        message_thread_id=callback.message.message_thread_id,
        expected_endpoint_id=expected_endpoint_id,
        new_endpoint_id=endpoint_id,
        model_name=new_model,
    )
    if not updated:
        await session.rollback()
        await callback.answer(
            "❌ Сессия или endpoint изменились. Откройте /model заново.",
            show_alert=True,
        )
        return
    await session.commit()
    logger.info(
        "Пользователь %s перепривязал сессию %s к endpoint %s и новой модели",
        callback.from_user.id, session_id, endpoint_id,
    )
    try:
        await callback.message.edit_text(
            "✅ <b>Endpoint и модель текущего топика изменены</b>\n\n"
            f"<b>Endpoint:</b> {escape_html(endpoint.name)}\n"
            f"<b>Модель:</b> <code>{escape_html(new_model)}</code>\n\n"
            "История сохранена. Глобальные настройки не изменены.",
            reply_markup=session_model_menu_keyboard(session_id),
            parse_mode="HTML",
        )
    except Exception as error:
        logger.warning(
            "Перепривязка сессии %s сохранена, но Telegram UI не обновлён: %s",
            session_id, type(error).__name__,
        )
        try:
            await callback.answer(
                "✅ Endpoint и модель сохранены, но меню не обновилось", show_alert=True
            )
        except Exception:
            pass
        return
    await callback.answer("✅ Endpoint и модель изменены")


@router.callback_query(F.data.regexp(r"^se:n:[0-9a-z]+:(n|[0-9a-z]+)$"))
async def callback_session_endpoint_noop(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    """Без изменений подтверждает актуальность rebind-меню для page indicator."""
    try:
        _, _, session_raw, old_raw = callback.data.split(":")
        session_id = _parse_callback_id(session_raw)
        expected_endpoint_id = _parse_expected_endpoint(old_raw)
    except (AttributeError, ValueError):
        await callback.answer("❌ Некорректное меню", show_alert=True)
        return
    owned = await _load_callback_session(
        callback, session, session_id, allow_missing_endpoint=True
    )
    if owned is None:
        return
    if owned.chat_session.endpoint_id != expected_endpoint_id:
        await callback.answer("❌ Меню устарело: сессия уже изменилась", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.regexp(r"^sm:m:[0-9]+$"))
async def callback_session_model_menu(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    try:
        session_id = int(callback.data.split(":")[2])
    except (AttributeError, IndexError, ValueError):
        await callback.answer("❌ Некорректное меню", show_alert=True)
        return
    owned = await _load_callback_session(callback, session, session_id)
    if owned is None:
        return
    await callback.message.edit_text(
        _session_model_text(owned),
        reply_markup=session_model_menu_keyboard(session_id),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^sm:f:[0-9]+$"))
async def callback_session_model_favorites(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    try:
        session_id = int(callback.data.split(":")[2])
    except (AttributeError, IndexError, ValueError):
        await callback.answer("❌ Некорректное меню", show_alert=True)
        return
    owned = await _load_callback_session(callback, session, session_id)
    if owned is None:
        return
    try:
        await _show_session_favorites(callback, session, owned)
    except Exception as error:
        logger.warning("Не удалось показать избранные модели сессии %s: %s", session_id, error)
        await callback.answer("❌ Не удалось обновить список моделей", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.regexp(r"^sm:l:[0-9]+:-?[0-9]+$"))
async def callback_session_models_list(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    try:
        _, _, session_raw, page_raw = callback.data.split(":")
        session_id = int(session_raw)
        page = int(page_raw)
        if page < 0:
            raise ValueError
    except (AttributeError, ValueError):
        await callback.answer("❌ Некорректная страница", show_alert=True)
        return
    owned = await _load_callback_session(callback, session, session_id)
    if owned is None:
        return
    try:
        models = await _available_session_models(session, owned)
    except Exception as error:
        logger.warning("Не удалось получить модели сессии %s: %s", session_id, error)
        await callback.answer("❌ Не удалось обновить список моделей", show_alert=True)
        return
    if not models:
        await callback.answer("❌ Endpoint не вернул доступных моделей", show_alert=True)
        return
    max_page = (len(models) - 1) // 8
    if page > max_page:
        await callback.answer("❌ Эта страница больше недоступна", show_alert=True)
        return
    current_model = owned.chat_session.model_name or owned.chat_session.model
    await callback.message.edit_text(
        _session_model_text(
            owned,
            section=f"📋 <b>Все модели:</b> страница {page + 1}/{max_page + 1}",
        ),
        reply_markup=session_models_list_keyboard(
            session_id, models, current_model, page=page
        ),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"^sm:s:[0-9]+:[0-9a-f]{12}:-?[0-9]+$"))
async def callback_session_model_select(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    try:
        _, _, session_raw, digest, page_raw = callback.data.split(":")
        session_id = int(session_raw)
        page = int(page_raw)
        if page < 0 or len(digest) != 12:
            raise ValueError
    except (AttributeError, ValueError):
        await callback.answer("❌ Некорректный выбор", show_alert=True)
        return
    owned = await _load_callback_session(callback, session, session_id)
    if owned is None:
        return
    try:
        models = await _available_session_models(session, owned)
    except Exception as error:
        logger.warning("Не удалось разрешить модель сессии %s: %s", session_id, error)
        await callback.answer("❌ Не удалось обновить список моделей", show_alert=True)
        return

    matches = [model for model in models if session_model_digest(model) == digest]
    if len(matches) != 1:
        logger.warning(
            "Digest модели не разрешён однозначно для сессии %s: digest=%s, matches=%s",
            session_id,
            digest,
            len(matches),
        )
        await callback.answer(
            "❌ Модель исчезла или идентификатор неоднозначен. Обновите список.",
            show_alert=True,
        )
        return

    new_model = matches[0]
    old_model = owned.chat_session.model_name or owned.chat_session.model
    if new_model == old_model:
        await callback.answer("ℹ️ Эта модель уже выбрана для топика", show_alert=True)
        return

    updated = await update_owned_chat_session_model(
        session,
        session_id=session_id,
        user_id=callback.from_user.id,
        chat_id=callback.message.chat.id,
        message_thread_id=callback.message.message_thread_id,
        endpoint_id=owned.endpoint.id,
        model_name=new_model,
    )
    if not updated:
        await session.rollback()
        await callback.answer("❌ Сессия изменилась, повторите выбор", show_alert=True)
        return
    await session.commit()
    logger.info(
        "Пользователь %s сменил модель сессии %s: %s -> %s",
        callback.from_user.id,
        session_id,
        old_model,
        new_model,
    )
    try:
        await callback.message.edit_text(
            "✅ <b>Модель текущего топика изменена</b>\n\n"
            f"<code>{escape_html(old_model)}</code> → <code>{escape_html(new_model)}</code>\n\n"
            "История диалога сохранена. Новая модель применяется к следующим запросам.",
            reply_markup=session_model_menu_keyboard(session_id),
            parse_mode="HTML",
        )
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error).lower():
            logger.warning(
                "Модель сессии %s сохранена, но Telegram UI не обновлён: %s",
                session_id,
                error,
            )
            await callback.answer(
                "✅ Модель сохранена, но меню не удалось обновить", show_alert=True
            )
            return
    except Exception as error:
        logger.warning(
            "Модель сессии %s сохранена, но Telegram UI недоступен: %s",
            session_id,
            error,
        )
        try:
            await callback.answer(
                "✅ Модель сохранена, но меню не удалось обновить", show_alert=True
            )
        except Exception:
            pass
        return
    await callback.answer("✅ Модель изменена")


@router.callback_query(F.data.regexp(r"^sm:n:[0-9]+$"))
async def callback_session_model_noop(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    try:
        session_id = int(callback.data.split(":")[2])
    except (AttributeError, IndexError, ValueError):
        await callback.answer("❌ Некорректное меню", show_alert=True)
        return
    owned = await _load_callback_session(callback, session, session_id)
    if owned is None:
        return
    await callback.answer()


@router.callback_query(F.data.regexp(r"^sm:x:[0-9]+$"))
async def callback_session_model_close(
    callback: CallbackQuery, session: AsyncSession
) -> None:
    try:
        session_id = int(callback.data.split(":")[2])
    except (AttributeError, IndexError, ValueError):
        await callback.answer("❌ Некорректное меню", show_alert=True)
        return
    owned = await _load_callback_session(
        callback, session, session_id, allow_missing_endpoint=True
    )
    if owned is None:
        return
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        pass
    await callback.answer()


@router.message(F.chat.type.in_({"supergroup", "group"}), F.message_thread_id)
async def handle_forum_message(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    user: User | None = None,
):
    """
    Хендлер сообщений в топиках форума с агрегацией.
    Добавляет сообщение в буфер агрегатора вместо немедленной обработки.
    
    Args:
        message: Сообщение от пользователя
        bot: Экземпляр aiogram.Bot
        session: DB сессия (не используется, т.к. агрегатор создаёт свою)
        user: Пользователь (прокинут через middleware или None)
    """
    # Получаем глобальный агрегатор
    aggregator = get_aggregator()
    
    # Добавляем сообщение в буфер (агрегатор вызовет process_message_batch после дебаунса)
    await aggregator.add_message(message, bot, process_message_batch)
    
    logger.debug(
        f"Сообщение от пользователя {message.from_user.id if message.from_user else 'unknown'} "
        f"добавлено в агрегатор для топика {message.message_thread_id}"
    )
