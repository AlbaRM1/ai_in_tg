"""
FSM состояния для настроек профиля.
Управление добавлением эндпоинтов через диалоговый интерфейс.
"""

from aiogram.fsm.state import State, StatesGroup


class EndpointStates(StatesGroup):
    """Состояния добавления нового эндпоинта"""
    
    waiting_for_name = State()      # ввод названия эндпоинта
    waiting_for_url = State()       # ввод base_url
    waiting_for_api_key = State()   # ввод API ключа
