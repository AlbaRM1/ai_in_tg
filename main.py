"""
Точка входа для Universal LLM Telegram Bot.
Запуск: python main.py
"""

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from app.config import settings
from app.database.base import init_db
from app.handlers import chat, registration, settings as settings_handler
from app.middlewares.auth import AuthMiddleware
from app.middlewares.database import DatabaseMiddleware
from app.services.web_search import is_web_search_enabled

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)


async def on_startup(bot: Bot) -> None:
    """Действия при запуске бота"""
    logger.info("Initializing database...")
    await init_db()
    logger.info("Database initialized successfully")

    # Информируем о статусе веб-поиска (Tavily): помогает диагностировать,
    # почему модель вызывает/не вызывает инструмент web_search.
    logger.info(
        "Web search: %s",
        "enabled (Tavily key set)" if is_web_search_enabled() else "disabled (no TAVILY_API_KEY)",
    )

    # Установка команд бота
    commands = [
        BotCommand(command="start", description="Регистрация с токеном"),
        BotCommand(command="settings", description="Настройка эндпоинтов и моделей"),
        BotCommand(command="generate_token", description="Генерация токена (только админ)"),
    ]
    await bot.set_my_commands(commands)
    logger.info("Bot commands set successfully")

    bot_info = await bot.get_me()
    logger.info(f"Bot started: @{bot_info.username} (ID: {bot_info.id})")


async def on_shutdown(bot: Bot) -> None:
    """Действия при остановке бота"""
    logger.info("Shutting down bot...")
    await bot.session.close()


async def main() -> None:
    """Главная функция запуска бота"""
    # Создаём бота
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Создаём диспетчер с FSM storage
    dp = Dispatcher(storage=MemoryStorage())

    # Регистрируем middleware в правильном порядке
    # DatabaseMiddleware раньше AuthMiddleware (auth нуждается в session)
    dp.message.middleware(DatabaseMiddleware())
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(DatabaseMiddleware())
    dp.callback_query.middleware(AuthMiddleware())

    # Регистрируем роутеры в порядке приоритета
    # registration первый (узкие фильтры /start, /generate_token)
    # settings второй (callback-хендлеры и /settings в приватном чате)
    # chat последний (широкий фильтр на топики в супергруппах)
    dp.include_router(registration.router)
    dp.include_router(settings_handler.router)
    dp.include_router(chat.router)

    # Регистрируем startup/shutdown хуки
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # Запускаем polling
    logger.info("Starting bot polling...")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped")
