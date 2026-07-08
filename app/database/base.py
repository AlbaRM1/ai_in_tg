"""
Настройка подключения к БД и session factory для SQLAlchemy 2.0 async.
Поддерживает PostgreSQL (asyncpg) и SQLite (aiosqlite) как запасной вариант.
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

# Определение диалекта БД по DATABASE_URL
DATABASE_URL_STR = str(settings.DATABASE_URL)
IS_SQLITE = DATABASE_URL_STR.startswith("sqlite")

# Создаём async engine с учётом диалекта
if IS_SQLITE:
    # SQLite: не используем pool-параметры, добавляем connect_args
    engine = create_async_engine(
        DATABASE_URL_STR,
        echo=False,
        connect_args={"check_same_thread": False},
    )
    logger.info("Используется SQLite (aiosqlite) в режиме запасной БД")
else:
    # PostgreSQL: используем pool-параметры
    engine = create_async_engine(
        DATABASE_URL_STR,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )
    logger.info("Используется PostgreSQL (asyncpg)")

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
    Поддерживает PostgreSQL и SQLite.
    """
    async with engine.begin() as conn:
        # Создаём таблицы (если их нет)
        await conn.run_sync(Base.metadata.create_all)
        logger.info("create_all выполнен")
    
    # Идемпотентное добавление новых колонок в chat_sessions
    # PostgreSQL: используем IF NOT EXISTS
    # SQLite: проверяем через PRAGMA и добавляем только отсутствующие
    if not IS_SQLITE:
        # PostgreSQL-специфичные ALTER TABLE с IF NOT EXISTS
        async with engine.begin() as conn:
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
                    logger.warning(
                        f"✗ Не удалось добавить колонку chat_sessions.{col_name}: {e}"
                    )
    else:
        # SQLite: проверяем существующие колонки через PRAGMA table_info
        async with engine.begin() as conn:
            try:
                result = await conn.execute(text("PRAGMA table_info(chat_sessions)"))
                existing_columns = {row[1] for row in result.fetchall()}
                
                # Список колонок для добавления (SQLite-совместимые типы)
                sqlite_columns = [
                    ("model_name", "VARCHAR(255)"),
                    ("endpoint_id", "INTEGER"),
                    ("pinned_message_id", "BIGINT"),
                    ("topic_renamed", "BOOLEAN DEFAULT 0"),  # SQLite: 0 = false
                ]
                
                for col_name, col_type in sqlite_columns:
                    if col_name not in existing_columns:
                        try:
                            stmt = text(
                                f"ALTER TABLE chat_sessions ADD COLUMN {col_name} {col_type}"
                            )
                            await conn.execute(stmt)
                            logger.info(f"✓ [SQLite] Добавлена колонка chat_sessions.{col_name}")
                        except Exception as e:
                            logger.warning(
                                f"✗ [SQLite] Не удалось добавить колонку {col_name}: {e}"
                            )
            except Exception as e:
                logger.warning(f"✗ [SQLite] Ошибка проверки колонок: {e}")


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
