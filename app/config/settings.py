"""
Конфигурация приложения на основе pydantic-settings.
Загружает параметры из переменных окружения (.env файл).
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения"""

    # Telegram Bot
    BOT_TOKEN: str = Field(..., description="Токен Telegram бота от @BotFather")
    ADMIN_TELEGRAM_ID: int = Field(
        ..., description="Telegram ID администратора для генерации токенов"
    )

    # Database
    DATABASE_URL: str = Field(
        ...,
        description="URL подключения к БД. PostgreSQL (основной): postgresql+asyncpg://user:pass@localhost/dbname; SQLite (запасной): sqlite+aiosqlite:///./bot.db",
    )

    # Security
    ENCRYPTION_KEY: str | None = Field(
        None,
        description="Ключ шифрования API-ключей (base64, 32 байта для Fernet). Пока не используется — заглушки.",
    )

    # Context Management
    MAX_CONTEXT_TOKENS: int = Field(
        default=100_000,
        description="Максимальное количество токенов в контексте. Консервативный "
        "дефолт (100k) с запасом под окно fallback-моделей и под неточность оценки "
        "токенов (особенно на кириллице). При переполнении история обрезается — "
        "отбрасываются самые старые сообщения, system prompt сохраняется.",
    )

    # LLM generation parameters
    LLM_TEMPERATURE: float | None = Field(
        default=None,
        description="Температура генерации LLM. По умолчанию None — параметр НЕ "
        "передаётся в запрос (важно: некоторые модели, напр. Claude Opus 4, "
        "возвращают ошибку 'temperature is deprecated for this model'). Задайте "
        "число (напр. 0.7), только если ваша модель точно поддерживает temperature.",
    )

    # Web Search (Tavily)
    TAVILY_API_KEY: str | None = Field(
        default=None,
        description="Ключ для веб-поиска через Tavily (https://tavily.com). "
        "Если не задан — веб-поиск отключён, бот работает как обычно.",
    )

    # Model configuration
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


# Singleton экземпляр настроек
settings = Settings()
