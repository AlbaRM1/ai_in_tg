"""
Конфигурация приложения на основе pydantic-settings.
Загружает параметры из переменных окружения (.env файл).
"""

from pydantic import Field, PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения"""

    # Telegram Bot
    BOT_TOKEN: str = Field(..., description="Токен Telegram бота от @BotFather")
    ADMIN_TELEGRAM_ID: int = Field(
        ..., description="Telegram ID администратора для генерации токенов"
    )

    # Database
    DATABASE_URL: PostgresDsn = Field(
        ...,
        description="URL подключения к PostgreSQL (asyncpg), например: postgresql+asyncpg://user:pass@localhost/dbname",
    )

    # Security
    ENCRYPTION_KEY: str | None = Field(
        None,
        description="Ключ шифрования API-ключей (base64, 32 байта для Fernet). Пока не используется — заглушки.",
    )

    # Context Management
    MAX_CONTEXT_TOKENS: int = Field(
        default=200_000,
        description="Максимальное количество токенов в контексте (200k по умолчанию)",
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
