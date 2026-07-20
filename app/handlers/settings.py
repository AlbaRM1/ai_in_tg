"""
Хендлеры настроек профиля.
Управление эндпоинтами, выбор моделей, избранное через inline-клавиатуры и FSM.
"""

import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.keyboards.inline import (
    confirm_delete_keyboard,
    endpoint_detail_keyboard,
    endpoints_list_keyboard,
    favorites_keyboard,
    main_settings_keyboard,
    models_list_keyboard,
)
from app.services.endpoint_service import (
    add_endpoint,
    add_favorite_model,
    delete_owned_endpoint,
    get_favorite_models,
    get_models_for_owned_endpoint,
    get_owned_endpoint,
    get_user_endpoints,
    is_favorite,
    remove_favorite_model,
    set_active_endpoint,
    validate_endpoint,
)
from app.services.user_service import get_user
from app.states.settings import EndpointStates
from app.utils.formatting import escape_html

logger = logging.getLogger(__name__)

router = Router(name="settings")


# ========================
# КОМАНДА /settings
# ========================

@router.message(Command("settings"), F.chat.type == "private")
async def cmd_settings(message: Message, session: AsyncSession) -> None:
    """
    Обработчик команды /settings.
    Отображает главное меню настроек профиля.
    """
    await message.answer(
        "⚙️ <b>Настройки профиля</b>\n\n"
        "Выберите раздел:",
        reply_markup=main_settings_keyboard()
    )
    logger.info(f"Пользователь {message.from_user.id} открыл настройки")


# ========================
# ГЛАВНОЕ МЕНЮ
# ========================

@router.callback_query(F.data == "settings:main")
async def callback_settings_main(callback: CallbackQuery) -> None:
    """Возврат в главное меню настроек"""
    try:
        await callback.message.edit_text(
            "⚙️ <b>Настройки профиля</b>\n\n"
            "Выберите раздел:",
            reply_markup=main_settings_keyboard()
        )
    except Exception as e:
        # Игнорируем ошибки "message is not modified"
        if "message is not modified" not in str(e).lower():
            logger.error(f"Ошибка редактирования сообщения: {e}")
    
    await callback.answer()


@router.callback_query(F.data == "settings:close")
async def callback_settings_close(callback: CallbackQuery) -> None:
    """Закрытие меню настроек"""
    await callback.message.delete()
    await callback.answer()
    logger.info(f"Пользователь {callback.from_user.id} закрыл настройки")


# ========================
# ЭНДПОИНТЫ - СПИСОК
# ========================

@router.callback_query(F.data == "endpoints:list")
async def callback_endpoints_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Отображение списка эндпоинтов пользователя"""
    user = await get_user(session, callback.from_user.id)
    endpoints = await get_user_endpoints(session, callback.from_user.id)
    
    if not endpoints:
        text = "🔌 <b>Мои эндпоинты</b>\n\nУ вас пока нет добавленных эндпоинтов."
    else:
        text = f"🔌 <b>Мои эндпоинты</b>\n\n<i>Всего эндпоинтов: {len(endpoints)}</i>"
    
    try:
        await callback.message.edit_text(
            text,
            reply_markup=endpoints_list_keyboard(endpoints, user.active_endpoint_id)
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Ошибка редактирования сообщения: {e}")
    
    await callback.answer()


# ========================
# ЭНДПОИНТЫ - ДЕТАЛИ
# ========================

@router.callback_query(F.data.startswith("endpoint:select:"))
async def callback_endpoint_select(callback: CallbackQuery, session: AsyncSession) -> None:
    """Отображение деталей конкретного эндпоинта"""
    endpoint_id = int(callback.data.split(":")[2])
    endpoint = await get_owned_endpoint(session, callback.from_user.id, endpoint_id)
    
    if endpoint is None:
        await callback.answer("❌ Эндпоинт не найден", show_alert=True)
        return
    
    # Проверяем, активен ли этот эндпоинт
    user = await get_user(session, callback.from_user.id)
    is_active = user.active_endpoint_id == endpoint_id
    
    # Скрываем API ключ
    masked_key = endpoint.api_key_encrypted[:8] + "..." if len(endpoint.api_key_encrypted) > 8 else "***"
    
    active_status = "✅ <b>Активный</b>" if is_active else "❌ Неактивный"
    
    text = (
        f"🔌 <b>{escape_html(endpoint.name)}</b>\n\n"
        f"<b>Статус:</b> {active_status}\n"
        f"<b>URL:</b> <code>{escape_html(endpoint.base_url)}</code>\n"
        f"<b>API ключ:</b> <code>{escape_html(masked_key)}</code>"
    )
    
    try:
        await callback.message.edit_text(
            text,
            reply_markup=endpoint_detail_keyboard(endpoint_id, is_active)
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Ошибка редактирования сообщения: {e}")
    
    await callback.answer()


# ========================
# ЭНДПОИНТЫ - АКТИВАЦИЯ
# ========================

@router.callback_query(F.data.startswith("endpoint:activate:"))
async def callback_endpoint_activate(callback: CallbackQuery, session: AsyncSession) -> None:
    """Активация эндпоинта"""
    endpoint_id = int(callback.data.split(":")[2])

    activated = await set_active_endpoint(session, callback.from_user.id, endpoint_id)
    if not activated:
        await session.rollback()
        await callback.answer("❌ Эндпоинт не найден", show_alert=True)
        return
    await session.commit()

    await callback.answer("✅ Эндпоинт активирован")
    logger.info(f"Пользователь {callback.from_user.id} активировал эндпоинт {endpoint_id}")
    
    # Обновляем отображение деталей
    await callback_endpoint_select(callback, session)


# ========================
# ЭНДПОИНТЫ - УДАЛЕНИЕ
# ========================

@router.callback_query(F.data.startswith("endpoint:delete:") & ~F.data.contains("confirm"))
async def callback_endpoint_delete(callback: CallbackQuery, session: AsyncSession) -> None:
    """Запрос подтверждения удаления эндпоинта"""
    endpoint_id = int(callback.data.split(":")[2])
    endpoint = await get_owned_endpoint(session, callback.from_user.id, endpoint_id)
    
    if endpoint is None:
        await callback.answer("❌ Эндпоинт не найден", show_alert=True)
        return
    
    text = (
        f"🗑 <b>Удаление эндпоинта</b>\n\n"
        f"Вы уверены, что хотите удалить эндпоинт <b>{escape_html(endpoint.name)}</b>?\n\n"
        f"<i>Это действие необратимо. Все избранные модели для этого эндпоинта также будут удалены.</i>"
    )
    
    try:
        await callback.message.edit_text(
            text,
            reply_markup=confirm_delete_keyboard(endpoint_id)
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Ошибка редактирования сообщения: {e}")
    
    await callback.answer()


@router.callback_query(F.data.startswith("endpoint:delete_confirm:"))
async def callback_endpoint_delete_confirm(callback: CallbackQuery, session: AsyncSession) -> None:
    """Подтверждённое удаление эндпоинта"""
    endpoint_id = int(callback.data.split(":")[2])
    
    success = await delete_owned_endpoint(
        session, callback.from_user.id, endpoint_id
    )
    if success:
        await session.commit()
        await callback.answer("✅ Эндпоинт удалён")
        logger.info(f"Пользователь {callback.from_user.id} удалил эндпоинт {endpoint_id}")
    else:
        await session.rollback()
        await callback.answer("❌ Эндпоинт не найден", show_alert=True)
    
    # Возвращаемся к списку эндпоинтов
    await callback_endpoints_list(callback, session)


# ========================
# ЭНДПОИНТЫ - ДОБАВЛЕНИЕ (FSM)
# ========================

@router.callback_query(F.data == "endpoint:add")
async def callback_endpoint_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало процесса добавления нового эндпоинта"""
    await state.set_state(EndpointStates.waiting_for_name)
    await callback.message.answer(
        "➕ <b>Добавление эндпоинта</b>\n\n"
        "Введите название эндпоинта (например, \"OpenAI\" или \"Local LLM\"):"
    )
    await callback.answer()
    logger.info(f"Пользователь {callback.from_user.id} начал добавление эндпоинта")


@router.message(EndpointStates.waiting_for_name)
async def process_endpoint_name(message: Message, state: FSMContext) -> None:
    """Обработка ввода названия эндпоинта"""
    name = message.text.strip()
    
    if len(name) < 1 or len(name) > 100:
        await message.answer("❌ Название должно быть от 1 до 100 символов. Попробуйте снова:")
        return
    
    await state.update_data(endpoint_name=name)
    await state.set_state(EndpointStates.waiting_for_url)
    
    await message.answer(
        "✅ Название сохранено.\n\n"
        "Теперь введите URL эндпоинта (например, <code>https://api.openai.com</code>):"
    )


@router.message(EndpointStates.waiting_for_url)
async def process_endpoint_url(message: Message, state: FSMContext) -> None:
    """Обработка ввода URL эндпоинта"""
    url = message.text.strip()
    
    # Базовая валидация URL
    if not url.startswith(("http://", "https://")):
        await message.answer(
            "❌ URL должен начинаться с <code>http://</code> или <code>https://</code>. "
            "Попробуйте снова:"
        )
        return
    
    await state.update_data(endpoint_url=url)
    await state.set_state(EndpointStates.waiting_for_api_key)
    
    await message.answer(
        "✅ URL сохранён.\n\n"
        "Теперь введите API ключ:\n\n"
        "<i>Ключ будет зашифрован и сохранён безопасно.</i>"
    )


@router.message(EndpointStates.waiting_for_api_key)
async def process_endpoint_api_key(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Обработка ввода API ключа и завершение добавления эндпоинта"""
    api_key = message.text.strip()
    
    if len(api_key) < 1:
        await message.answer("❌ API ключ не может быть пустым. Попробуйте снова:")
        return
    
    # Получаем сохранённые данные
    data = await state.get_data()
    name = data.get("endpoint_name")
    url = data.get("endpoint_url")
    
    # Валидируем эндпоинт
    status_msg = await message.answer("⏳ Проверяю эндпоинт...")
    
    is_valid, error_message, model_ids = await validate_endpoint(url, api_key)
    
    if not is_valid:
        await status_msg.delete()
        await message.answer(
            f"❌ <b>Ошибка валидации эндпоинта:</b>\n\n"
            f"{error_message}\n\n"
            f"Проверьте URL и API ключ, затем попробуйте снова.\n\n"
            f"Для отмены отправьте /settings"
        )
        await state.clear()
        logger.warning(
            f"Пользователь {message.from_user.id} не смог добавить эндпоинт: {error_message}"
        )
        return
    
    # Добавляем эндпоинт
    endpoint = await add_endpoint(session, message.from_user.id, name, url, api_key)
    await session.commit()
    
    await status_msg.delete()
    await message.answer(
        f"✅ <b>Эндпоинт добавлен!</b>\n\n"
        f"<b>Название:</b> {escape_html(name)}\n"
        f"<b>URL:</b> <code>{escape_html(url)}</code>\n"
        f"<b>Найдено моделей:</b> {len(model_ids)}\n\n"
        f"Теперь вы можете активировать его и выбрать модель.",
        reply_markup=main_settings_keyboard()
    )
    
    await state.clear()
    logger.info(
        f"Пользователь {message.from_user.id} добавил эндпоинт {endpoint.id} '{name}'"
    )


# ========================
# МОДЕЛИ - СПИСОК
# ========================

@router.callback_query(F.data == "models:list")
async def callback_models_list(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    """Отображение списка моделей для выбора (первая страница)"""
    # Перенаправляем на страницу 0
    await callback_models_list_page(callback, session, state, page=0)


async def callback_models_list_page(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext,
    page: int = 0
) -> None:
    """
    Отображение списка моделей с пагинацией.
    
    Args:
        callback: Callback query
        session: DB сессия
        state: FSM контекст
        page: Номер страницы (начиная с 0)
    """
    user = await get_user(session, callback.from_user.id)
    
    # Проверяем наличие активного эндпоинта
    if user.active_endpoint_id is None:
        await callback.answer(
            "❌ Сначала выберите активный эндпоинт в разделе \"Мои эндпоинты\"",
            show_alert=True
        )
        return
    
    # Получаем список моделей (пробуем из состояния, иначе загружаем)
    data = await state.get_data()
    models = data.get("models_list")
    cached_endpoint_id = data.get("active_endpoint_id")
    
    # Если модели не в кэше или сменился эндпоинт, загружаем заново
    if not models or cached_endpoint_id != user.active_endpoint_id:
        try:
            models = await get_models_for_owned_endpoint(
                session, callback.from_user.id, user.active_endpoint_id
            )
        except Exception as e:
            logger.error(f"Ошибка получения моделей для эндпоинта {user.active_endpoint_id}: {e}")
            await callback.answer(
                f"❌ Ошибка получения списка моделей:\n{str(e)}",
                show_alert=True
            )
            return
        
        if not models:
            await callback.answer("❌ Не удалось получить список моделей", show_alert=True)
            return
        
        # Сохраняем список моделей в состоянии
        await state.update_data(models_list=models, active_endpoint_id=user.active_endpoint_id)
    
    # Получаем список избранных моделей для текущего эндпоинта
    favorites_list = await get_favorite_models(session, callback.from_user.id)
    favorites = {
        fav.model_name
        for fav in favorites_list
        if fav.endpoint_id == user.active_endpoint_id
    }
    
    # Формируем текст сообщения
    endpoint = await get_owned_endpoint(
        session, callback.from_user.id, user.active_endpoint_id
    )
    endpoint_name = endpoint.name if endpoint else "Unknown"
    
    text = (
        f"🤖 <b>Выбор модели</b>\n\n"
        f"<b>Активный эндпоинт:</b> {escape_html(endpoint_name)}\n"
        f"<b>Доступно моделей:</b> {len(models)}"
    )
    
    try:
        await callback.message.edit_text(
            text,
            reply_markup=models_list_keyboard(models, user.active_model, favorites, page=page)
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Ошибка редактирования сообщения: {e}")
    
    await callback.answer()


# ========================
# МОДЕЛИ - НАВИГАЦИЯ ПО СТРАНИЦАМ
# ========================

@router.callback_query(F.data.startswith("models:page:"))
async def callback_models_page(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    """Навигация по страницам списка моделей"""
    page_str = callback.data.split(":")[2]
    
    # Игнорируем noop (индикатор страницы)
    if page_str == "noop":
        await callback.answer()
        return
    
    try:
        page = int(page_str)
    except ValueError:
        await callback.answer("❌ Некорректная страница", show_alert=True)
        return
    
    # Отображаем нужную страницу
    await callback_models_list_page(callback, session, state, page=page)


# ========================
# МОДЕЛИ - ВЫБОР
# ========================

@router.callback_query(F.data.startswith("model:select:"))
async def callback_model_select(callback: CallbackQuery, session: AsyncSession, state: FSMContext) -> None:
    """Выбор активной модели"""
    index = int(callback.data.split(":")[2])
    
    # Получаем список моделей из состояния
    data = await state.get_data()
    models = data.get("models_list", [])
    
    if index < 0 or index >= len(models):
        await callback.answer("❌ Модель не найдена", show_alert=True)
        return
    
    model_name = models[index]
    
    # Устанавливаем активную модель
    user = await get_user(session, callback.from_user.id)
    user.active_model = model_name
    await session.commit()
    
    await callback.answer(f"✅ Модель выбрана: {model_name}")
    logger.info(f"Пользователь {callback.from_user.id} выбрал модель {model_name}")
    
    # Вычисляем текущую страницу по индексу модели (размер страницы = 8)
    current_page = index // 8
    
    # Обновляем клавиатуру с сохранением текущей страницы
    await callback_models_list_page(callback, session, state, page=current_page)


# ========================
# МОДЕЛИ - TOGGLE ИЗБРАННОЕ
# ========================

@router.callback_query(F.data.startswith("model:fav:"))
async def callback_model_favorite_toggle(
    callback: CallbackQuery,
    session: AsyncSession,
    state: FSMContext
) -> None:
    """Toggle избранного для модели"""
    index = int(callback.data.split(":")[2])
    
    # Получаем список моделей из состояния
    data = await state.get_data()
    models = data.get("models_list", [])
    endpoint_id = data.get("active_endpoint_id")
    
    if index < 0 or index >= len(models):
        await callback.answer("❌ Модель не найдена", show_alert=True)
        return
    
    model_name = models[index]
    user_id = callback.from_user.id
    
    # Проверяем, в избранном ли модель
    if await is_favorite(session, user_id, endpoint_id, model_name):
        # Удаляем из избранного
        await remove_favorite_model(session, user_id, endpoint_id, model_name)
        await session.commit()
        await callback.answer(f"☆ Модель удалена из избранного")
        logger.info(f"Пользователь {user_id} удалил модель {model_name} из избранного")
    else:
        # Добавляем в избранное
        await add_favorite_model(session, user_id, endpoint_id, model_name)
        await session.commit()
        await callback.answer(f"⭐ Модель добавлена в избранное")
        logger.info(f"Пользователь {user_id} добавил модель {model_name} в избранное")
    
    # Вычисляем текущую страницу по индексу модели (размер страницы = 8)
    current_page = index // 8
    
    # Обновляем клавиатуру с сохранением текущей страницы
    await callback_models_list_page(callback, session, state, page=current_page)


# ========================
# ИЗБРАННОЕ - СПИСОК
# ========================

@router.callback_query(F.data == "fav:list")
async def callback_favorites_list(callback: CallbackQuery, session: AsyncSession) -> None:
    """Отображение списка избранных моделей"""
    favorites = await get_favorite_models(session, callback.from_user.id)
    
    if not favorites:
        text = "⭐ <b>Избранные модели</b>\n\nУ вас пока нет избранных моделей."
    else:
        text = f"⭐ <b>Избранные модели</b>\n\n<i>Всего избранных: {len(favorites)}</i>"
    
    try:
        await callback.message.edit_text(
            text,
            reply_markup=favorites_keyboard(favorites)
        )
    except Exception as e:
        if "message is not modified" not in str(e).lower():
            logger.error(f"Ошибка редактирования сообщения: {e}")
    
    await callback.answer()


# ========================
# ИЗБРАННОЕ - ВЫБОР
# ========================

@router.callback_query(F.data.startswith("fav:select:"))
async def callback_favorite_select(callback: CallbackQuery, session: AsyncSession) -> None:
    """Выбор модели из избранного"""
    fav_id = int(callback.data.split(":")[2])
    
    # Получаем избранную модель
    favorites = await get_favorite_models(session, callback.from_user.id)
    favorite = next((fav for fav in favorites if fav.id == fav_id), None)
    
    if favorite is None:
        await callback.answer("❌ Избранная модель не найдена", show_alert=True)
        return

    # Favorite owner-scoped, но endpoint мог исчезнуть между экранами.
    endpoint = await get_owned_endpoint(
        session, callback.from_user.id, favorite.endpoint_id
    )
    if endpoint is None:
        await session.rollback()
        await callback.answer("❌ Эндпоинт этой модели больше недоступен", show_alert=True)
        return

    # Устанавливаем активный эндпоинт и модель только после повторной owner-проверки.
    user = await get_user(session, callback.from_user.id)
    user.active_endpoint_id = favorite.endpoint_id
    user.active_model = favorite.model_name
    await session.commit()

    endpoint_name = endpoint.name
    
    await callback.answer(
        f"✅ Активирован эндпоинт \"{endpoint_name}\" и модель \"{favorite.model_name}\""
    )
    logger.info(
        f"Пользователь {callback.from_user.id} выбрал избранное: "
        f"эндпоинт {favorite.endpoint_id}, модель {favorite.model_name}"
    )
    
    # Возвращаемся к списку избранного
    await callback_favorites_list(callback, session)
