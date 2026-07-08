"""
Хендлеры регистрации и административных команд.
Обработка команд /start с токеном и /generate_token для админа.
"""

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import user_service

logger = logging.getLogger(__name__)

# Создаём router для регистрационных хендлеров
router = Router(name="registration")


@router.message(Command("start"))
async def cmd_start(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """
    Обработчик команды /start.
    
    - Если пользователь уже зарегистрирован — приветствие
    - Если аргумент отсутствует — инструкция по регистрации
    - Если аргумент есть — попытка регистрации
    """
    telegram_id = message.from_user.id
    logger.info(f"Команда /start от пользователя {telegram_id}, args: {command.args}")
    
    # Проверяем, зарегистрирован ли пользователь
    is_registered = await user_service.is_registered(session, telegram_id)
    
    if is_registered:
        # Пользователь уже зарегистрирован
        await message.answer(
            "Вы уже зарегистрированы. Используйте /settings для настройки."
        )
        return
    
    # Проверяем наличие аргумента (токена)
    if not command.args:
        # Аргумент отсутствует — показываем инструкцию
        await message.answer(
            "👋 Добро пожаловать! Для регистрации отправьте: <code>/start ваш_токен</code>. "
            "Токен можно получить у администратора."
        )
        return
    
    # Есть аргумент — пытаемся зарегистрировать
    token_str = command.args.strip()
    success, msg = await user_service.register_user(session, telegram_id, token_str)
    
    if success:
        logger.info(f"Пользователь {telegram_id} успешно зарегистрирован")
        await message.answer(f"✅ {msg}")
    else:
        logger.warning(f"Ошибка регистрации пользователя {telegram_id}: {msg}")
        await message.answer(f"❌ {msg}")


@router.message(Command("generate_token"))
async def cmd_generate_token(
    message: Message,
    session: AsyncSession,
) -> None:
    """
    Обработчик команды /generate_token.
    Генерирует новый токен регистрации.
    
    Доступна только администратору.
    """
    telegram_id = message.from_user.id
    logger.info(f"Команда /generate_token от пользователя {telegram_id}")
    
    # Проверяем, является ли пользователь администратором
    if not await user_service.is_admin(telegram_id):
        logger.warning(f"Попытка неадминистратора {telegram_id} сгенерировать токен")
        await message.answer("🚫 Команда доступна только администратору.")
        return
    
    # Генерируем токен
    token = await user_service.generate_token(session)
    
    # Отправляем токен администратору
    await message.answer(
        f"🎫 Новый токен регистрации:\n"
        f"<code>{token.token}</code>\n\n"
        f"Передайте его пользователю для регистрации через <code>/start токен</code>"
    )
    logger.info(f"Администратор {telegram_id} сгенерировал токен {token.token}")
