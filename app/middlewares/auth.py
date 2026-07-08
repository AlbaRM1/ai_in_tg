"""
Middleware авторизации пользователей.
Проверяет регистрацию и блокирует доступ незарегистрированным пользователям.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.services import user_service

logger = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    """
    Middleware для проверки регистрации пользователя.
    
    - Пропускает команду /start (для регистрации)
    - Пропускает администратора
    - Блокирует незарегистрированных пользователей с инструкцией
    - Прокидывает в data флаг is_admin и объект user
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # --- Фильтрация сервисных сообщений и сообщений без пользователя/от ботов ---
        if isinstance(event, Message):
            # Игнорируем сервисные сообщения форума и чата (не пользовательский ввод)
            _SERVICE_FIELDS = (
                "forum_topic_created",
                "forum_topic_edited",
                "forum_topic_closed",
                "forum_topic_reopened",
                "general_forum_topic_hidden",
                "general_forum_topic_unhidden",
                "pinned_message",
                "new_chat_members",
                "left_chat_member",
                "new_chat_title",
                "new_chat_photo",
                "delete_chat_photo",
                "group_chat_created",
                "supergroup_chat_created",
                "channel_chat_created",
                "message_auto_delete_timer_changed",
                "boost_added",
            )
            for _field in _SERVICE_FIELDS:
                if getattr(event, _field, None) is not None:
                    logger.debug(f"Игнорируем сервисное сообщение (поле: {_field})")
                    return None

            # Игнорируем сообщения без пользователя или от ботов
            if event.from_user is None:
                logger.debug("Игнорируем сообщение без from_user (анонимный/системный)")
                return None
            if event.from_user.is_bot:
                logger.debug(f"Игнорируем сообщение от бота {event.from_user.id}")
                return None

        # Получаем session из data (DatabaseMiddleware выполняется раньше)
        session = data.get("session")
        if not session:
            logger.error("Session не найдена в data. DatabaseMiddleware должен выполняться раньше AuthMiddleware.")
            return
        
        # Определяем telegram_id из события
        telegram_id = None
        if isinstance(event, (Message, CallbackQuery)):
            telegram_id = event.from_user.id
        
        if telegram_id is None:
            logger.warning("Не удалось извлечь telegram_id из события")
            return await handler(event, data)
        
        # Проверяем, является ли пользователь администратором
        is_admin_flag = await user_service.is_admin(telegram_id)
        data["is_admin"] = is_admin_flag
        
        # Пропускаем админа без дополнительных проверок
        if is_admin_flag:
            logger.debug(f"Администратор {telegram_id} пропущен без проверки регистрации")
            user = await user_service.get_user(session, telegram_id)
            data["user"] = user
            return await handler(event, data)
        
        # Пропускаем команду /start (для возможности регистрации)
        if isinstance(event, Message):
            if event.text and event.text.startswith("/start"):
                logger.debug(f"Команда /start от {telegram_id}, пропускаем без проверки")
                user = await user_service.get_user(session, telegram_id)
                data["user"] = user
                return await handler(event, data)
        
        # Проверяем регистрацию пользователя
        is_registered = await user_service.is_registered(session, telegram_id)
        
        if not is_registered:
            logger.info(f"Незарегистрированный пользователь {telegram_id} попытался получить доступ")
            
            # Отправляем сообщение о необходимости регистрации
            if isinstance(event, Message):
                await event.answer(
                    "🚫 Вы не зарегистрированы. Используйте <code>/start токен</code> для регистрации."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer(
                    "🚫 Вы не зарегистрированы. Используйте /start токен для регистрации.",
                    show_alert=True
                )
            
            # Не вызываем handler
            return
        
        # Пользователь зарегистрирован, получаем объект и прокидываем дальше
        user = await user_service.get_user(session, telegram_id)
        data["user"] = user
        logger.debug(f"Зарегистрированный пользователь {telegram_id} допущен к хендлеру")
        
        return await handler(event, data)
