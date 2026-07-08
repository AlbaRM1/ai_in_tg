"""
Настройка подключения к БД и session factory для SQLAlchemy 2.0 async.
"""

import logging
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.database.models import Base

logger = logging.getLogger(__name__)

# Создаём async engine
engine = create_async_engine(
    str(settings.DATABASE_URL),
    echo=False,  # Установите True для отладки SQL-запросов
    pool_pre_ping=True,  # Проверка соединения перед использованием
    pool_size=10,
    max_overflow=20,
)

# Session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def init_db() -> None:
    """
    Инициализация БД: создание всех таблиц.
    Использовать только для разработки. В проде — Alembic миграции.
    
    После create_all выполняет идемпотентные ALTER TABLE для добавления
    новых колонок (совместимость с существующими БД).
    """
    async with engine.begin() as conn:
        # Создаём таблицы (если их нет)
        await conn.run_sync(Base.metadata.create_all)
        logger.info("create_all выполнен")
    
    # Идемпотентное добавление новых колонок в chat_sessions
    # Выполняется в отдельной транзакции для надёжности
    async with engine.begin() as conn:
        # Список колонок для добавления (без FK constraint для простоты)
        alter_columns = [
            ("model_name", "VARCHAR(255)"),
            ("endpoint_id", "INTEGER"),
            ("pinned_message_id", "BIGINT"),
            ("topic_renamed", "BOOLEAN DEFAULT FALSE"),
        ]
        
        for col_name, col_type in alter_columns:
            try:
                stmt = text(
                    f"ALTER TABLE chat_sessions "
                    f"ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                )
                await conn.execute(stmt)
                logger.info(f"✓ Добавлена колонка chat_sessions.{col_name} ({col_type})")
            except Exception as e:
                # Логируем ошибки, но не прерываем инициализацию
                logger.warning(
                    f"✗ Не удалось добавить колонку chat_sessions.{col_name}: {e}"
                )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency injection для получения сессии БД.
    Используется в middleware для прокидывания сессии в хендлеры.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
