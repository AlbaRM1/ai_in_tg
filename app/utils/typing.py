"""
Утилита для управления typing-индикатором в Telegram.
Фоновая задача, которая отправляет chat action каждые ~4 секунды.
"""

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)


class TypingIndicator:
    """
    Фоновая задача для отображения "typing..." в Telegram чате.
    Telegram автоматически скрывает индикатор через ~5 секунд, поэтому отправляем каждые 4 сек.
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        message_thread_id: int | None = None,
        interval: float = 4.0,
    ):
        """
        Args:
            bot: Экземпляр aiogram Bot
            chat_id: ID чата
            message_thread_id: ID топика (для forum)
            interval: Интервал отправки typing action в секундах
        """
        self.bot = bot
        self.chat_id = chat_id
        self.message_thread_id = message_thread_id
        self.interval = interval
        self._task: asyncio.Task | None = None
        self._stopped = False

    async def _send_typing_loop(self) -> None:
        """Внутренний цикл отправки typing action"""
        while not self._stopped:
            try:
                await self.bot.send_chat_action(
                    chat_id=self.chat_id,
                    action="typing",
                    message_thread_id=self.message_thread_id,
                )
                logger.debug(f"Sent typing action to chat {self.chat_id}, thread {self.message_thread_id}")
            except TelegramBadRequest as e:
                logger.warning(f"Failed to send typing action: {e}")
                # Не останавливаем цикл, просто логируем
            except Exception as e:
                logger.error(f"Unexpected error in typing loop: {e}", exc_info=True)

            await asyncio.sleep(self.interval)

    def start(self) -> None:
        """Запускает фоновую задачу typing-индикатора"""
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self._send_typing_loop())
            logger.debug("Typing indicator started")

    def stop(self) -> None:
        """Останавливает typing-индикатор"""
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            logger.debug("Typing indicator stopped")

    async def __aenter__(self) -> "TypingIndicator":
        """Context manager: запуск при входе"""
        self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager: остановка при выходе"""
        self.stop()
        # Ждём отмены задачи
        if self._task:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
