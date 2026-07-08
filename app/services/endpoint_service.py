"""
Сервис управления эндпоинтами пользователя.
Инкапсулирует CRUD-операции с эндпоинтами и работу с моделями.
"""

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Endpoint, FavoriteModel, User
from app.services.llm_service import fetch_available_models
from app.utils.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)


async def add_endpoint(
    session: AsyncSession, user_id: int, name: str, base_url: str, api_key: str
) -> Endpoint:
    """
    Создаёт новый эндпоинт для пользователя.

    Args:
        session: Сессия БД
        user_id: ID пользователя (telegram_id)
        name: Название эндпоинта
        base_url: URL эндпоинта
        api_key: API ключ (будет зашифрован)

    Returns:
        Созданный объект Endpoint
    """
    # Шифруем API ключ перед сохранением
    encrypted_key = encrypt(api_key)

    endpoint = Endpoint(
        user_id=user_id,
        name=name,
        base_url=base_url.rstrip("/"),
        api_key_encrypted=encrypted_key,
    )

    session.add(endpoint)
    await session.flush()  # Получаем ID без коммита
    await session.refresh(endpoint)

    logger.info(f"Создан эндпоинт {endpoint.id} '{name}' для пользователя {user_id}")
    return endpoint


async def get_user_endpoints(session: AsyncSession, user_id: int) -> list[Endpoint]:
    """
    Возвращает все эндпоинты пользователя.

    Args:
        session: Сессия БД
        user_id: ID пользователя

    Returns:
        Список эндпоинтов пользователя
    """
    result = await session.execute(
        select(Endpoint).where(Endpoint.user_id == user_id).order_by(Endpoint.created_at)
    )
    endpoints = result.scalars().all()
    return list(endpoints)


async def get_endpoint(session: AsyncSession, endpoint_id: int) -> Endpoint | None:
    """
    Возвращает эндпоинт по ID.

    Args:
        session: Сессия БД
        endpoint_id: ID эндпоинта

    Returns:
        Объект Endpoint или None, если не найден
    """
    result = await session.execute(select(Endpoint).where(Endpoint.id == endpoint_id))
    return result.scalar_one_or_none()


async def delete_endpoint(session: AsyncSession, endpoint_id: int) -> bool:
    """
    Удаляет эндпоинт по ID.

    Args:
        session: Сессия БД
        endpoint_id: ID эндпоинта

    Returns:
        True, если эндпоинт был удалён, False, если не найден
    """
    endpoint = await get_endpoint(session, endpoint_id)
    if endpoint is None:
        logger.warning(f"Попытка удаления несуществующего эндпоинта {endpoint_id}")
        return False

    await session.delete(endpoint)
    await session.flush()
    logger.info(f"Эндпоинт {endpoint_id} удалён")
    return True


async def set_active_endpoint(session: AsyncSession, user_id: int, endpoint_id: int) -> None:
    """
    Устанавливает активный эндпоинт для пользователя.

    Args:
        session: Сессия БД
        user_id: ID пользователя
        endpoint_id: ID эндпоинта для активации
    """
    result = await session.execute(select(User).where(User.telegram_id == user_id))
    user = result.scalar_one_or_none()

    if user is None:
        logger.error(f"Пользователь {user_id} не найден при установке активного эндпоинта")
        raise ValueError(f"Пользователь {user_id} не найден")

    user.active_endpoint_id = endpoint_id
    await session.flush()
    logger.info(f"Для пользователя {user_id} установлен активный эндпоинт {endpoint_id}")


async def validate_endpoint(base_url: str, api_key: str) -> tuple[bool, str, list[str]]:
    """
    Проверяет доступность эндпоинта и возвращает список моделей.

    Args:
        base_url: URL эндпоинта
        api_key: API ключ

    Returns:
        Кортеж (успех, сообщение, список_id_моделей):
        - успех: True, если эндпоинт доступен
        - сообщение: Сообщение об ошибке или "OK"
        - список_id_моделей: Список ID доступных моделей
    """
    try:
        # Получаем список моделей через fetch_available_models
        models = await fetch_available_models(base_url, api_key, timeout=30)

        # Извлекаем ID моделей
        model_ids = [model.get("id", "") for model in models if "id" in model]

        if not model_ids:
            return False, "Эндпоинт не вернул список моделей", []

        logger.info(f"Эндпоинт {base_url} валиден, найдено {len(model_ids)} моделей")
        return True, "OK", model_ids

    except httpx.HTTPStatusError as e:
        # Обработка HTTP ошибок
        if e.response.status_code == 401:
            msg = "Неверный API ключ"
        elif e.response.status_code == 404:
            msg = "Эндпоинт не найден"
        elif e.response.status_code == 403:
            msg = "Доступ запрещён"
        else:
            msg = f"HTTP ошибка: {e.response.status_code}"

        logger.warning(f"Ошибка валидации эндпоинта {base_url}: {msg}")
        return False, msg, []

    except httpx.TimeoutException:
        msg = "Превышен таймаут запроса"
        logger.warning(f"Таймаут при валидации эндпоинта {base_url}")
        return False, msg, []

    except httpx.ConnectError as e:
        msg = "Не удалось подключиться к эндпоинту (проверьте URL)"
        logger.warning(f"Ошибка подключения к {base_url}: {e}")
        return False, msg, []

    except Exception as e:
        msg = f"Неизвестная ошибка: {str(e)}"
        logger.error(f"Неожиданная ошибка при валидации {base_url}: {e}", exc_info=True)
        return False, msg, []


async def get_models_for_endpoint(session: AsyncSession, endpoint_id: int) -> list[str]:
    """
    Получает список доступных моделей для эндпоинта.

    Args:
        session: Сессия БД
        endpoint_id: ID эндпоинта

    Returns:
        Список ID моделей

    Raises:
        ValueError: Если эндпоинт не найден
    """
    endpoint = await get_endpoint(session, endpoint_id)
    if endpoint is None:
        raise ValueError(f"Эндпоинт {endpoint_id} не найден")

    # Расшифровываем API ключ
    api_key = decrypt(endpoint.api_key_encrypted)

    # Получаем модели
    models = await fetch_available_models(endpoint.base_url, api_key, timeout=30)

    # Извлекаем ID моделей
    model_ids = [model.get("id", "") for model in models if "id" in model]

    logger.info(f"Для эндпоинта {endpoint_id} получено {len(model_ids)} моделей")
    return model_ids


async def add_favorite_model(
    session: AsyncSession, user_id: int, endpoint_id: int, model_name: str
) -> FavoriteModel:
    """
    Добавляет модель в избранное.

    Args:
        session: Сессия БД
        user_id: ID пользователя
        endpoint_id: ID эндпоинта
        model_name: Название модели

    Returns:
        Созданный объект FavoriteModel
    """
    favorite = FavoriteModel(
        user_id=user_id,
        endpoint_id=endpoint_id,
        model_name=model_name,
    )

    session.add(favorite)
    await session.flush()
    await session.refresh(favorite)

    logger.info(f"Модель '{model_name}' добавлена в избранное пользователя {user_id}")
    return favorite


async def remove_favorite_model(
    session: AsyncSession, user_id: int, endpoint_id: int, model_name: str
) -> bool:
    """
    Удаляет модель из избранного.

    Args:
        session: Сессия БД
        user_id: ID пользователя
        endpoint_id: ID эндпоинта
        model_name: Название модели

    Returns:
        True, если модель была удалена, False, если не найдена
    """
    result = await session.execute(
        select(FavoriteModel).where(
            FavoriteModel.user_id == user_id,
            FavoriteModel.endpoint_id == endpoint_id,
            FavoriteModel.model_name == model_name,
        )
    )
    favorite = result.scalar_one_or_none()

    if favorite is None:
        logger.warning(
            f"Попытка удаления несуществующей избранной модели '{model_name}' "
            f"пользователя {user_id}, эндпоинт {endpoint_id}"
        )
        return False

    await session.delete(favorite)
    await session.flush()
    logger.info(f"Модель '{model_name}' удалена из избранного пользователя {user_id}")
    return True


async def get_favorite_models(session: AsyncSession, user_id: int) -> list[FavoriteModel]:
    """
    Возвращает все избранные модели пользователя.

    Args:
        session: Сессия БД
        user_id: ID пользователя

    Returns:
        Список избранных моделей
    """
    result = await session.execute(
        select(FavoriteModel).where(FavoriteModel.user_id == user_id)
    )
    favorites = result.scalars().all()
    return list(favorites)


async def is_favorite(
    session: AsyncSession, user_id: int, endpoint_id: int, model_name: str
) -> bool:
    """
    Проверяет, находится ли модель в избранном.

    Args:
        session: Сессия БД
        user_id: ID пользователя
        endpoint_id: ID эндпоинта
        model_name: Название модели

    Returns:
        True, если модель в избранном, иначе False
    """
    result = await session.execute(
        select(FavoriteModel).where(
            FavoriteModel.user_id == user_id,
            FavoriteModel.endpoint_id == endpoint_id,
            FavoriteModel.model_name == model_name,
        )
    )
    favorite = result.scalar_one_or_none()
    return favorite is not None
