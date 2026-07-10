"""
Хендлер чата со streaming для работы в топиках форума.
Поддержка текста, изображений (photo + document-картинки) и документов (PDF, текстовые файлы).
Модель и эндпоинт фиксируются за сессией при первом сообщении.
Агрегация входящих сообщений (буферизация с дебаунсом) для обработки батчей.
"""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.types import Message
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.base import async_session_factory
from app.database.models import ChatSession, User
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
from app.services.endpoint_service import get_endpoint
from app.services.llm_service import LLMService
from app.services.message_aggregator import get_aggregator
from app.services.user_service import get_user
from app.services.web_search import is_web_search_enabled
from app.utils.crypto import decrypt
from app.utils.formatting import escape_html, format_for_telegram, sanitize_for_streaming, split_html_for_telegram, split_plain_text
from app.utils.typing import TypingIndicator

logger = logging.getLogger(__name__)

router = Router()


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
                
                # Фиксируем за сессией
                chat_session.model_name = session_model
                chat_session.endpoint_id = session_endpoint_id
                await session.flush()
                is_new_session = True
                
                logger.info(
                    f"Зафиксирована модель {session_model} за существующей сессией {chat_session.id}"
                )
            
            # 4. Получаем эндпоинт
            if session_endpoint_id:
                endpoint = await get_endpoint(session, session_endpoint_id)
                if not endpoint:
                    logger.warning(
                        f"Зафиксированный эндпоинт {session_endpoint_id} для сессии "
                        f"{chat_session.id} не найден. Переключение на активный эндпоинт пользователя."
                    )
                    
                    if not user.active_endpoint_id:
                        await first_message.reply(
                            "❌ Эндпоинт этого чата был удалён, и у вас нет активного эндпоинта. "
                            "Настройте эндпоинт через /settings."
                        )
                        return
                    
                    endpoint = await get_endpoint(session, user.active_endpoint_id)
                    if not endpoint:
                        await first_message.reply(
                            "❌ Активный эндпоинт не найден. Настройте через /settings"
                        )
                        return
                    
                    chat_session.endpoint_id = user.active_endpoint_id
                    await session.flush()
            else:
                if not user.active_endpoint_id:
                    await first_message.reply(
                        "⚠️ Сначала настройте эндпоинт через /settings в личке с ботом."
                    )
                    return
                endpoint = await get_endpoint(session, user.active_endpoint_id)
                if not endpoint:
                    await first_message.reply(
                        "❌ Активный эндпоинт не найден. Настройте через /settings"
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

            # 11. Загружаем полную историю
            history = await load_session_history(session, chat_session)

            # 12. Создаём LLM service
            llm = LLMService(
                base_url=endpoint.base_url,
                api_key=api_key,
                timeout=120,
            )

            # 13. Streaming генерация с typing-индикатором
            accumulated_text = ""
            last_update_time = asyncio.get_event_loop().time()
            update_interval = 1.5  # секунды между обновлениями сообщения
            # Безопасный лимит для предпросмотра во время стриминга (резерв под возможные символы)
            STREAMING_PREVIEW_LIMIT = 3800

            # Отправляем начальное сообщение (отвечаем на первое сообщение батча)
            reply_msg = await first_message.reply("💭 Думаю...")

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

                                await reply_msg.edit_text(
                                    sanitize_for_streaming(preview_text)
                                )
                                last_update_time = current_time
                                force_next_update = False
                            except TelegramRetryAfter as e:
                                # Rate limit от Telegram — ждём и продолжаем
                                logger.warning(
                                    f"Rate limit при streaming edit: retry_after={e.retry_after}s"
                                )
                                await asyncio.sleep(e.retry_after)
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
                                        # Edit не удался — отправляем новым сообщением
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
            logger.error(
                f"Непредвиденная ошибка при обработке батча сообщений: {e}",
                exc_info=True,
            )
            error_text = str(e).replace("&", "&").replace("<", "<").replace(">", ">")
            try:
                await first_message.reply(f"❌ Непредвиденная ошибка: {error_text}")
            except Exception as reply_error:
                logger.error(f"Не удалось отправить сообщение об ошибке: {reply_error}")


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
