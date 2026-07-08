"""
Фабрики inline-клавиатур для настроек профиля.
Создание клавиатур для навигации по меню эндпоинтов, моделей и избранного.
"""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_settings_keyboard() -> InlineKeyboardMarkup:
    """
    Главное меню настроек профиля.
    
    Returns:
        Inline-клавиатура с основными разделами настроек
    """
    builder = InlineKeyboardBuilder()
    
    builder.button(text="🔌 Мои эндпоинты", callback_data="endpoints:list")
    builder.button(text="🤖 Выбрать модель", callback_data="models:list")
    builder.button(text="⭐ Избранные модели", callback_data="fav:list")
    builder.button(text="❌ Закрыть", callback_data="settings:close")
    
    # Располагаем кнопки в столбец (по 1 в ряд)
    builder.adjust(1)
    
    return builder.as_markup()


def endpoints_list_keyboard(endpoints: list, active_endpoint_id: int | None) -> InlineKeyboardMarkup:
    """
    Клавиатура со списком эндпоинтов пользователя.
    
    Args:
        endpoints: Список объектов Endpoint
        active_endpoint_id: ID активного эндпоинта или None
        
    Returns:
        Inline-клавиатура со списком эндпоинтов
    """
    builder = InlineKeyboardBuilder()
    
    # Добавляем кнопку для каждого эндпоинта
    for endpoint in endpoints:
        prefix = "✅ " if endpoint.id == active_endpoint_id else ""
        builder.button(
            text=f"{prefix}{endpoint.name}",
            callback_data=f"endpoint:select:{endpoint.id}"
        )
    
    # Кнопки управления
    builder.button(text="➕ Добавить эндпоинт", callback_data="endpoint:add")
    builder.button(text="⬅️ Назад", callback_data="settings:main")
    
    # По 1 кнопке в ряд
    builder.adjust(1)
    
    return builder.as_markup()


def endpoint_detail_keyboard(endpoint_id: int, is_active: bool) -> InlineKeyboardMarkup:
    """
    Клавиатура с действиями для конкретного эндпоинта.
    
    Args:
        endpoint_id: ID эндпоинта
        is_active: Является ли эндпоинт активным
        
    Returns:
        Inline-клавиатура с действиями над эндпоинтом
    """
    builder = InlineKeyboardBuilder()
    
    # Кнопка активации (если не активен)
    if not is_active:
        builder.button(
            text="✅ Сделать активным",
            callback_data=f"endpoint:activate:{endpoint_id}"
        )
    
    # Кнопка удаления
    builder.button(text="🗑 Удалить", callback_data=f"endpoint:delete:{endpoint_id}")
    
    # Кнопка "Назад"
    builder.button(text="⬅️ Назад", callback_data="endpoints:list")
    
    # По 1 кнопке в ряд
    builder.adjust(1)
    
    return builder.as_markup()


def models_list_keyboard(
    models: list[str],
    active_model: str | None,
    favorites: set[str],
    page: int = 0,
    page_size: int = 8
) -> InlineKeyboardMarkup:
    """
    Клавиатура со списком моделей для выбора с пагинацией.
    
    Args:
        models: Список названий моделей
        active_model: Название активной модели или None
        favorites: Множество названий избранных моделей
        page: Номер текущей страницы (начиная с 0)
        page_size: Количество моделей на странице
        
    Returns:
        Inline-клавиатура со списком моделей с навигацией
    """
    builder = InlineKeyboardBuilder()
    
    # Вычисляем границы текущей страницы
    start_idx = page * page_size
    end_idx = start_idx + page_size
    total_pages = (len(models) + page_size - 1) // page_size  # Округление вверх
    
    # Модели текущей страницы
    models_to_show = models[start_idx:end_idx]
    
    # Добавляем кнопки для каждой модели на текущей странице
    for i, model_name in enumerate(models_to_show):
        # Глобальный индекс модели в полном списке
        global_index = start_idx + i
        
        # Префиксы для активной и избранной модели
        active_prefix = "✅ " if model_name == active_model else ""
        fav_prefix = "⭐ " if model_name in favorites else ""
        
        # Обрезаем длинное название модели для отображения
        display_name = model_name
        if len(display_name) > 35:
            display_name = display_name[:32] + "..."
        
        # Кнопка выбора модели (используем глобальный индекс)
        builder.button(
            text=f"{active_prefix}{fav_prefix}{display_name}",
            callback_data=f"model:select:{global_index}"
        )
        
        # Кнопка toggle избранного (используем глобальный индекс)
        star_icon = "⭐" if model_name in favorites else "☆"
        builder.button(
            text=star_icon,
            callback_data=f"model:fav:{global_index}"
        )
    
    # Навигационные кнопки (если страниц больше одной)
    nav_buttons = []
    if total_pages > 1:
        # Кнопка "Назад" (если не первая страница)
        if page > 0:
            nav_buttons.append(("⬅️ Назад", f"models:page:{page - 1}"))
        
        # Индикатор страницы
        nav_buttons.append((f"· {page + 1}/{total_pages} ·", "models:page:noop"))
        
        # Кнопка "Вперёд" (если не последняя страница)
        if page < total_pages - 1:
            nav_buttons.append(("Вперёд ➡️", f"models:page:{page + 1}"))
    
    # Добавляем навигационные кнопки
    for text, callback in nav_buttons:
        builder.button(text=text, callback_data=callback)
    
    # Кнопка "Назад в меню"
    builder.button(text="⬅️ Назад в меню", callback_data="settings:main")
    
    # Располагаем: модели по 2 кнопки в ряд (выбор | звёздочка),
    # навигация в один ряд, кнопка "Назад" отдельно
    adjust_pattern = [2] * len(models_to_show)  # По 2 кнопки для каждой модели
    if nav_buttons:
        adjust_pattern.append(len(nav_buttons))  # Навигационные кнопки в один ряд
    adjust_pattern.append(1)  # Кнопка "Назад в меню" отдельно
    
    builder.adjust(*adjust_pattern)
    
    return builder.as_markup()


def favorites_keyboard(favorites: list) -> InlineKeyboardMarkup:
    """
    Клавиатура со списком избранных моделей.
    
    Args:
        favorites: Список объектов FavoriteModel
        
    Returns:
        Inline-клавиатура с избранными моделями
    """
    builder = InlineKeyboardBuilder()
    
    # Добавляем кнопку для каждой избранной модели
    for fav in favorites:
        # Обрезаем длинное название модели
        display_name = fav.model_name
        if len(display_name) > 40:
            display_name = display_name[:37] + "..."
        
        builder.button(
            text=f"⭐ {display_name}",
            callback_data=f"fav:select:{fav.id}"
        )
    
    # Кнопка "Назад"
    builder.button(text="⬅️ Назад", callback_data="settings:main")
    
    # По 1 кнопке в ряд
    builder.adjust(1)
    
    return builder.as_markup()


def confirm_delete_keyboard(endpoint_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура подтверждения удаления эндпоинта.
    
    Args:
        endpoint_id: ID эндпоинта для удаления
        
    Returns:
        Inline-клавиатура с кнопками подтверждения
    """
    builder = InlineKeyboardBuilder()
    
    builder.button(
        text="✅ Да, удалить",
        callback_data=f"endpoint:delete_confirm:{endpoint_id}"
    )
    builder.button(
        text="❌ Отмена",
        callback_data=f"endpoint:select:{endpoint_id}"
    )
    
    # По 1 кнопке в ряд
    builder.adjust(1)
    
    return builder.as_markup()
