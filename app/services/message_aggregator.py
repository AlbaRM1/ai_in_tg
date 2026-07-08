"""
Сервис агрегации (буферизации) входящих сообщений в Telegram-боте.

Решает проблемы:
- Telegram режет длинные/пересланные тексты на несколько сообщений
- Альбомы (media-group) приходят как отдельные сообщения
- Флуд-ошибки при множественных быстрых ответах

Механизм:
- Буферизация по ключу (chat_id, message_thread_id, user_id)
- Дебаунс ~1.5 секунды: таймер сбрасывается при каждом новом сообщении
- После тишины батч обрабатывается одним вызовом LLM
- Автоматическая склейка текстов и вложений из всех сообщений батча
"""

import asyncio
import logging
from collections import defaultdict
from typing import Callable, Awaitable

from aiogram import Bot
from aiogram.types import Message

logger = logging.getLogger(__name__)

# Время дебаунса в секундах
DEBOUNCE_DELAY = 1.5

# Тип колбэка для обработки батча
BatchProcessorCallback = Callable[[list[Message], Bot], Awaitable[None]]


class MessageAggregator:
    """
    Агрегатор входящих сообщений с дебаунсом.
    
    Собирает сообщения одного пользователя в одном топике в буфер
    и обрабатывает их батчем после периода тишины.
    """
    
    def __init__(self):
        """Инициализация агрегатора"""
        # Буферы сообщений: {key: [Message, ...]}
        self._buffers: dict[tuple[int, int, int], list[Message]] = defaultdict(list)
        
        # Таймеры дебаунса: {key: asyncio.Task}
        self._timers: dict[tuple[int, int, int], asyncio.Task] = {}
        
        # Блокировки для потокобезопасности: {key: asyncio.Lock}
        self._locks: dict[tuple[int, int, int], asyncio.Lock] = defaultdict(asyncio.Lock)
        
        logger.info("MessageAggregator инициализирован")
    
    def _make_key(self, message: Message) -> tuple[int, int, int]:
        """
        Создаёт ключ буфера из сообщения.
        
        Args:
            message: Входящее сообщение
            
        Returns:
            Кортеж (chat_id, message_thread_id, user_id)
        """
        chat_id = message.chat.id
        thread_id = message.message_thread_id or 0
        user_id = message.from_user.id if message.from_user else 0
        return (chat_id, thread_id, user_id)
    
    async def add_message(
        self,
        message: Message,
        bot: Bot,
        processor: BatchProcessorCallback,
    ) -> None:
        """
        Добавляет сообщение в буфер и запускает/перезапускает таймер дебаунса.
        
        Args:
            message: Входящее сообщение
            bot: Экземпляр aiogram.Bot
            processor: Колбэк для обработки батча сообщений
        """
        key = self._make_key(message)
        
        async with self._locks[key]:
            # Добавляем сообщение в буфер
            self._buffers[key].append(message)
            
            logger.debug(
                f"Сообщение добавлено в буфер {key}, "
                f"всего в буфере: {len(self._buffers[key])}"
            )
            
            # Отменяем существующий таймер (если есть)
            if key in self._timers:
                existing_timer = self._timers[key]
                if not existing_timer.done():
                    existing_timer.cancel()
                    logger.debug(f"Таймер для {key} отменён (новое сообщение)")
            
            # Создаём новый таймер дебаунса
            timer_task = asyncio.create_task(
                self._debounce_timer(key, bot, processor)
            )
            self._timers[key] = timer_task
            
            logger.debug(f"Таймер дебаунса {DEBOUNCE_DELAY}с запущен для {key}")
    
    async def _debounce_timer(
        self,
        key: tuple[int, int, int],
        bot: Bot,
        processor: BatchProcessorCallback,
    ) -> None:
        """
        Таймер дебаунса: ждёт DEBOUNCE_DELAY секунд тишины, затем обрабатывает батч.
        
        Args:
            key: Ключ буфера (chat_id, thread_id, user_id)
            bot: Экземпляр aiogram.Bot
            processor: Колбэк для обработки батча
        """
        try:
            # Ждём период дебаунса
            await asyncio.sleep(DEBOUNCE_DELAY)
            
            # После тишины — обрабатываем батч
            async with self._locks[key]:
                batch = self._buffers[key].copy()
                
                # Очищаем буфер и таймер
                del self._buffers[key]
                if key in self._timers:
                    del self._timers[key]
                
                logger.info(
                    f"Дебаунс завершён для {key}, обработка батча из {len(batch)} сообщений"
                )
            
            # Обрабатываем батч вне блокировки (может занять время)
            if batch:
                try:
                    await processor(batch, bot)
                except Exception as e:
                    logger.error(
                        f"Ошибка обработки батча для {key}: {e}",
                        exc_info=True,
                    )
        
        except asyncio.CancelledError:
            # Таймер отменён новым сообщением — нормальное поведение
            logger.debug(f"Таймер дебаунса отменён для {key}")
        except Exception as e:
            logger.error(
                f"Непредвиденная ошибка в таймере дебаунса для {key}: {e}",
                exc_info=True,
            )


# Глобальный экземпляр агрегатора (singleton)
_aggregator: MessageAggregator | None = None


def get_aggregator() -> MessageAggregator:
    """
    Получает глобальный экземпляр агрегатора (создаёт при первом вызове).
    
    Returns:
        MessageAggregator: Глобальный агрегатор сообщений
    """
    global _aggregator
    if _aggregator is None:
        _aggregator = MessageAggregator()
    return _aggregator
