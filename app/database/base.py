"""
Настройка подключения к БД и session factory для SQLAlchemy 2.0 async.
Поддерживает PostgreSQL (asyncpg) и SQLite (aiosqlite) как запасной вариант.
Managed-PostgreSQL (Neon, Supabase) с SSL и pooler-совместимостью.
"""

import logging
import ssl
from collections.abc import AsyncGenerator
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import settings
from app.database.models import Base

logger = logging.getLogger(__name__)


def _normalize_database_url(url: str) -> str:
    """
    Нормализует DATABASE_URL: заменяет голую схему 'postgresql://' на
    'postgresql+asyncpg://', чтобы SQLAlchemy не пытался загрузить
    синхронный psycopg2 по умолчанию.

    Если указана схема 'postgres://' (устаревший Heroku-формат) — тоже
    исправляем.  Все остальные схемы (sqlite+aiosqlite://, уже правильный
    postgresql+asyncpg://) возвращаются без изменений.
    """
    for bare in ("postgres://", "postgresql://"):
        if url.startswith(bare):
            fixed = "postgresql+asyncpg://" + url[len(bare):]
            logger.warning(
                "DATABASE_URL содержит схему '%s' без указания драйвера. "
                "Автоматически заменено на 'postgresql+asyncpg://'. "
                "Укажите явный драйвер в DATABASE_URL, чтобы убрать это предупреждение.",
                bare,
            )
            return fixed
    return url


# Определение диалекта БД по DATABASE_URL
DATABASE_URL_STR = _normalize_database_url(str(settings.DATABASE_URL))
IS_SQLITE = DATABASE_URL_STR.startswith("sqlite")


def _prepare_postgres_url_and_args(url: str) -> tuple[str, dict]:
    """
    Подготовка PostgreSQL URL и connect_args для managed-провайдеров (Neon/Supabase).
    
    asyncpg не понимает libpq-параметры (sslmode, channel_binding) в URL.
    Извлекаем sslmode из query-строки и конвертируем в SSL-контекст для connect_args.
    Удаляем несовместимые параметры из URL.
    
    Args:
        url: исходный DATABASE_URL (postgresql+asyncpg://...)
    
    Returns:
        tuple: (cleaned_url, connect_args_dict)
    """
    parts = urlsplit(url)
    query_params = parse_qs(parts.query, keep_blank_values=True)
    
    # Извлекаем sslmode (если есть)
    sslmode_list = query_params.pop("sslmode", None)
    sslmode = sslmode_list[0] if sslmode_list else None
    
    # Удаляем другие libpq-параметры, которые asyncpg не понимает
    query_params.pop("channel_binding", None)
    
    # Собираем очищенный URL
    new_query = urlencode(query_params, doseq=True)
    cleaned_parts = parts._replace(query=new_query)
    cleaned_url = urlunsplit(cleaned_parts)
    
    # Настройка connect_args
    connect_args = {
        # Для Neon pooler (pgbouncer в transaction mode) отключаем statement cache
        "statement_cache_size": 0,
    }
    
    # SSL: если sslmode указан, создаём SSL-контекст
    if sslmode in ("require", "verify-ca", "verify-full"):
        ssl_context = ssl.create_default_context()
        # Для "require" разрешаем self-signed сертификаты
        if sslmode == "require":
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ssl_context
        logger.info(f"SSL включён для PostgreSQL (sslmode={sslmode})")
    
    return cleaned_url, connect_args


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
    # PostgreSQL: подготовка URL и connect_args для managed-провайдеров
    cleaned_url, pg_connect_args = _prepare_postgres_url_and_args(DATABASE_URL_STR)
    
    engine = create_async_engine(
        cleaned_url,
        echo=False,
        pool_pre_ping=True,  # Важно для бесплатных тиров (они засыпают)
        pool_size=10,
        max_overflow=20,
        pool_recycle=300,  # Переиспользовать соединения максимум 5 минут
        connect_args=pg_connect_args,
    )
    logger.info("Используется PostgreSQL (asyncpg) с поддержкой managed-провайдеров")

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
