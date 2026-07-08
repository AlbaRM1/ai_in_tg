"""
Бизнес-логика пользователей и токенов регистрации.
Обработка регистрации, проверка токенов, управление пользователями.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.database.models import RegistrationToken, User

logger = logging.getLogger(__name__)


async def get_user(session: AsyncSession, telegram_id: int) -> User | None:
    """
    Получить пользователя по Telegram ID.
    
    Args:
        session: Сессия БД
        telegram_id: Telegram ID пользователя
        
    Returns:
        Объект User или None, если не найден
    """
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()
    logger.debug(f"Получен пользователь {telegram_id}: {user is not None}")
    return user


async def is_registered(session: AsyncSession, telegram_id: int) -> bool:
    """
    Проверить, зарегистрирован ли пользователь.
    
    Args:
        session: Сессия БД
        telegram_id: Telegram ID пользователя
        
    Returns:
        True, если пользователь существует в таблице users
    """
    user = await get_user(session, telegram_id)
    is_reg = user is not None
    logger.debug(f"Пользователь {telegram_id} зарегистрирован: {is_reg}")
    return is_reg


async def generate_token(session: AsyncSession) -> RegistrationToken:
    """
    Создать новый одноразовый токен регистрации.
    UUID генерируется автоматически в модели.
    
    Args:
        session: Сессия БД
        
    Returns:
        Созданный объект RegistrationToken
    """
    token = RegistrationToken()
    session.add(token)
    await session.flush()  # Чтобы получить сгенерированный UUID
    logger.info(f"Создан новый токен регистрации: {token.token}")
    return token


async def register_user(
    session: AsyncSession, telegram_id: int, token_str: str
) -> tuple[bool, str]:
    """
    Регистрация пользователя по одноразовому токену.
    
    Args:
        session: Сессия БД
        telegram_id: Telegram ID пользователя
        token_str: Строка с токеном (UUID)
        
    Returns:
        (success: bool, message: str) - результат регистрации
    """
    # Проверяем, не зарегистрирован ли пользователь уже
    if await is_registered(session, telegram_id):
        logger.warning(f"Попытка повторной регистрации пользователя {telegram_id}")
        return False, "Вы уже зарегистрированы"
    
    # Парсим UUID из строки
    try:
        token_uuid = uuid.UUID(token_str)
    except ValueError:
        logger.warning(f"Неверный формат токена от пользователя {telegram_id}: {token_str}")
        return False, "Неверный формат токена"
    
    # Ищем токен в базе
    result = await session.execute(
        select(RegistrationToken).where(RegistrationToken.token == token_uuid)
    )
    token = result.scalar_one_or_none()
    
    if token is None:
        logger.warning(f"Токен не найден: {token_uuid}")
        return False, "Токен не найден"
    
    if token.is_used:
        logger.warning(f"Попытка использовать уже использованный токен: {token_uuid}")
        return False, "Токен уже использован"
    
    # Создаём пользователя
    new_user = User(telegram_id=telegram_id)
    session.add(new_user)
    
    # Помечаем токен как использованный
    token.is_used = True
    token.used_by_telegram_id = telegram_id
    
    await session.flush()
    logger.info(f"Успешная регистрация пользователя {telegram_id} с токеном {token_uuid}")
    
    return True, "Регистрация успешна!"


async def is_admin(telegram_id: int) -> bool:
    """
    Проверить, является ли пользователь администратором.
    
    Args:
        telegram_id: Telegram ID пользователя
        
    Returns:
        True, если пользователь является администратором
    """
    is_adm = telegram_id == settings.ADMIN_TELEGRAM_ID
    logger.debug(f"Пользователь {telegram_id} является админом: {is_adm}")
    return is_adm
