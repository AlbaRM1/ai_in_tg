"""
Хендлер чата со streaming для работы в топиках форума.
Поддержка текста, изображений (photo + document-картинки) и документов (PDF, текстовые файлы).
Модель и эндпоинт фиксируются за сессией при первом сообщении.
"""

import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.types import Message
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.services.user_service import get_user
from app.utils.crypto import decrypt
from app.utils.formatting import escape_html, format_for_telegram, sanitize_for_streaming
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


@router.message(F.chat.type.in_({"supergroup", "group"}), F.message_thread_id)
async def handle_forum_message(
    message: Message,
    bot: Bot,
    session: AsyncSession,
    user: User | None = None,
):
    """
    Обработчик сообщений в топиках форума со streaming.
    Поддерживает текст, изображения и документы.
    
    Args:
        message: Сообщение от пользователя
        bot: Экземпляр aiogram.Bot
        session: DB сессия
        user: Пользователь (прокинут через middleware или None)
    """
    try:
        user_id = message.from_user.id
        chat_id = message.chat.id
        thread_id = message.message_thread_id

        # 1. Получаем пользователя
        if user is None:
            user = await get_user(session, user_id)
        
        if not user:
            await message.reply(
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
        # Если модель уже зафиксирована за сессией — используем её
        # Иначе используем текущую активную модель пользователя
        if chat_session.model_name:
            # Сессия уже имеет зафиксированную модель
            session_model = chat_session.model_name
            session_endpoint_id = chat_session.endpoint_id
        else:
            # Первое сообщение в существующей сессии (миграция со старой схемы)
            # Фиксируем текущую модель пользователя
            if not user.active_endpoint_id or not user.active_model:
                await message.reply(
                    "⚠️ Сначала настройте эндпоинт и модель через /settings в личке с ботом."
                )
                return
            
            session_model = user.active_model
            session_endpoint_id = user.active_endpoint_id
            
            # Фиксируем за сессией
            chat_session.model_name = session_model
            chat_session.endpoint_id = session_endpoint_id
            await session.flush()
            is_new_session = True  # Считаем как новую для закрепления инфо
            
            logger.info(
                f"Зафиксирована модель {session_model} за существующей сессией {chat_session.id}"
            )
        
        # 4. Получаем эндпоинт (зафиксированный за сессией или текущий активный)
        if session_endpoint_id:
            endpoint = await get_endpoint(session, session_endpoint_id)
            if not endpoint:
                # Зафиксированный эндпоинт был удалён
                logger.warning(
                    f"Зафиксированный эндпоинт {session_endpoint_id} для сессии "
                    f"{chat_session.id} не найден. Переключение на активный эндпоинт пользователя."
                )
                
                # Fallback: используем текущий активный эндпоинт
                if not user.active_endpoint_id:
                    await message.reply(
                        "❌ Эндпоинт этого чата был удалён, и у вас нет активного эндпоинта. "
                        "Настройте эндпоинт через /settings."
                    )
                    return
                
                endpoint = await get_endpoint(session, user.active_endpoint_id)
                if not endpoint:
                    await message.reply(
                        "❌ Активный эндпоинт не найден. Настройте через /settings"
                    )
                    return
                
                # Обновляем сессию на новый эндпоинт
                chat_session.endpoint_id = user.active_endpoint_id
                await session.flush()
        else:
            # На случай если endpoint_id не задан (не должно случиться при новой логике)
            if not user.active_endpoint_id:
                await message.reply(
                    "⚠️ Сначала настройте эндпоинт через /settings в личке с ботом."
                )
                return
            endpoint = await get_endpoint(session, user.active_endpoint_id)
            if not endpoint:
                await message.reply(
                    "❌ Активный эндпоинт не найден. Настройте через /settings"
                )
                return

        api_key = decrypt(endpoint.api_key_encrypted)

        # 5. Закрепляем инфо-сообщение о модели (только для новой сессии)
        if is_new_session and not chat_session.pinned_message_id:
            try:
                # Отправляем инфо-сообщение
                model_display_name = escape_html(session_model)
                info_text = f"🤖 Модель этого чата: <code>{model_display_name}</code>"
                
                info_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=info_text,
                    parse_mode="HTML",
                    message_thread_id=thread_id,
                )
                
                # Пытаемся закрепить сообщение
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
                    # Не удалось закрепить (нет прав) — не критично
                    logger.warning(
                        f"Не удалось закрепить инфо-сообщение для сессии {chat_session.id}: "
                        f"{pin_error}. Сообщение отправлено, но не закреплено."
                    )
                    # Всё равно сохраняем message_id для отслеживания
                    chat_session.pinned_message_id = info_msg.message_id
                    await session.flush()
            except Exception as info_error:
                # Ошибка отправки инфо-сообщения — логируем, но продолжаем работу
                logger.error(
                    f"Не удалось отправить инфо-сообщение о модели для сессии "
                    f"{chat_session.id}: {info_error}",
                    exc_info=True,
                )
        
        # 6. Автопереименование топика по первому сообщению
        if not chat_session.topic_renamed and message.message_thread_id:
            try:
                # Формируем краткое название из текста первого сообщения
                user_text = message.text or message.caption or ""
                
                if not user_text.strip():
                    # Нет текста — определяем тип контента
                    if message.photo:
                        topic_name = "🖼 Изображение"
                    elif message.document:
                        topic_name = f"📄 {message.document.file_name or 'Документ'}"
                    else:
                        topic_name = "💬 Диалог"
                else:
                    # Обрезаем текст для названия топика
                    # Telegram лимит: 1-128 символов
                    max_length = 50
                    topic_name = user_text.replace("\n", " ").strip()
                    if len(topic_name) > max_length:
                        topic_name = topic_name[:max_length].rstrip() + "…"
                
                # Пытаемся переименовать топик
                await bot.edit_forum_topic(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    name=topic_name,
                )
                
                # Устанавливаем флаг успешного переименования
                chat_session.topic_renamed = True
                await session.flush()
                
                logger.info(
                    f"Топик {thread_id} переименован в '{topic_name}' для сессии {chat_session.id}"
                )
            except (TelegramBadRequest, TelegramForbiddenError) as rename_error:
                # Нет прав can_manage_topics или это General-топик
                logger.warning(
                    f"Не удалось переименовать топик {thread_id} для сессии {chat_session.id}: "
                    f"{rename_error}. Продолжаем без переименования."
                )
                # Устанавливаем флаг чтобы не пытаться повторно
                chat_session.topic_renamed = True
                await session.flush()
            except Exception as rename_error:
                # Другие ошибки — логируем и продолжаем
                logger.error(
                    f"Ошибка при переименовании топика {thread_id}: {rename_error}",
                    exc_info=True,
                )
                # Устанавливаем флаг чтобы не пытаться повторно
                chat_session.topic_renamed = True
                await session.flush()
        
        # 7. Собираем content_parts из сообщения
        content_parts: list[dict] = []
        
        # Текст (из text или caption)
        text = message.text or message.caption or ""
        if text.strip():
            content_parts.append(build_text_part(text))
        
        # Фото (берём наибольшее)
        if message.photo:
            try:
                photo = message.photo[-1]  # Последнее = наибольшее разрешение
                logger.info(f"Обработка фото: file_id={photo.file_id}")
                image_part = await process_photo(bot, photo.file_id, "image/jpeg")
                content_parts.append(image_part)
            except Exception as e:
                logger.error(f"Ошибка обработки фото: {e}", exc_info=True)
                await message.reply(
                    f"❌ Не удалось обработать изображение: {str(e)}"
                )
                return
        
        # Документ
        if message.document:
            try:
                doc = message.document
                logger.info(
                    f"Обработка документа: {doc.file_name}, "
                    f"mime_type={doc.mime_type}"
                )
                doc_part = await process_document(
                    bot,
                    doc.file_id,
                    doc.file_name,
                    doc.mime_type,
                )
                content_parts.append(doc_part)
            except Exception as e:
                logger.error(f"Ошибка обработки документа: {e}", exc_info=True)
                await message.reply(
                    f"❌ Не удалось обработать документ: {str(e)}"
                )
                return
        
        # Если ничего не распознано — игнорируем
        if not content_parts:
            logger.debug(
                f"Пустое сообщение от пользователя {user_id}, пропускаем"
            )
            return
        
        # 8. Определяем тип контента
        # Если только один text-part → сохраняем как обычный текст
        # Иначе → мультимодальный формат
        is_multimodal = len(content_parts) > 1 or (
            len(content_parts) == 1 and content_parts[0].get("type") != "text"
        )
        
        # Текстовая выжимка для проверки контекста и сохранения
        text_summary = " ".join(
            part.get("text", "")
            for part in content_parts
            if part.get("type") == "text"
        )
        if not text_summary.strip():
            text_summary = "[мультимодальное сообщение без текста]"

        # 9. Проверяем, что новое сообщение поместится в контекст
        if not await ensure_context_fits(session, chat_session, text_summary):
            await message.reply(
                "❌ Сообщение слишком длинное и не помещается в контекст даже "
                "после сжатия. Попробуйте сократить запрос или начните новый топик."
            )
            return

        # 10. Сохраняем сообщение пользователя
        await add_message_to_session(
            session,
            chat_session,
            "user",
            text_summary,
            content_parts=content_parts if is_multimodal else None,
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

        # Отправляем начальное сообщение
        reply_msg = await message.reply("💭 Думаю...")

        try:
            async with TypingIndicator(bot, chat_id, thread_id):
                async for token in llm.stream_chat_completion(
                    model=session_model,  # Используем зафиксированную модель сессии
                    messages=history,
                ):
                    accumulated_text += token

                    # Throttled update: обновляем раз в 1.5 сек
                    current_time = asyncio.get_event_loop().time()
                    if current_time - last_update_time >= update_interval:
                        try:
                            # Во время streaming отправляем plain text
                            await reply_msg.edit_text(
                                sanitize_for_streaming(accumulated_text)
                            )
                            last_update_time = current_time
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
            error_text = str(e).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            await reply_msg.edit_text(f"❌ Ошибка генерации: {error_text}")
            return

        # 14. Финальное обновление с полным HTML-форматированием
        if accumulated_text.strip():
            formatted_text = format_for_telegram(accumulated_text)
            try:
                await reply_msg.edit_text(formatted_text, parse_mode="HTML")
            except TelegramRetryAfter as e:
                # Rate limit — ждём и пробуем ещё раз
                logger.warning(
                    f"Rate limit при финальном edit: retry_after={e.retry_after}s, "
                    "повторная попытка..."
                )
                await asyncio.sleep(e.retry_after)
                try:
                    await reply_msg.edit_text(formatted_text, parse_mode="HTML")
                except TelegramBadRequest as bad_request_error:
                    logger.error(
                        f"Невалидный HTML после повторной попытки: {bad_request_error}. "
                        "Отправка без форматирования."
                    )
                    # Fallback: отправляем без parse_mode
                    await reply_msg.edit_text(accumulated_text)
                except Exception as retry_error:
                    logger.error(
                        f"Повторная попытка финального edit не удалась: {retry_error}"
                    )
                    # Fallback: отправляем без форматирования
                    await reply_msg.edit_text(accumulated_text)
            except TelegramBadRequest as e:
                logger.error(
                    f"Невалидный HTML в финальном сообщении: {e}. "
                    "Отправка без форматирования."
                )
                # Fallback: отправляем без parse_mode
                try:
                    await reply_msg.edit_text(accumulated_text)
                except Exception as fallback_error:
                    logger.error(
                        f"Fallback edit тоже не удался: {fallback_error}"
                    )
            except Exception as e:
                logger.error(f"Не удалось отформатировать финальное сообщение: {e}")
                # Fallback: отправляем без форматирования
                try:
                    await reply_msg.edit_text(accumulated_text)
                except Exception as fallback_error:
                    logger.error(
                        f"Fallback edit тоже не удался: {fallback_error}"
                    )
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
            f"топик {thread_id}, {len(accumulated_text)} символов"
        )

    except Exception as e:
        logger.error(
            f"Непредвиденная ошибка в хендлере сообщений форума: {e}",
            exc_info=True,
        )
        error_text = str(e).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        try:
            await message.reply(f"❌ Непредвиденная ошибка: {error_text}")
        except Exception as reply_error:
            logger.error(f"Не удалось отправить сообщение об ошибке: {reply_error}")
