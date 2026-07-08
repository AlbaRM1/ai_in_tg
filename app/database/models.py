"""
SQLAlchemy 2.0 async модели базы данных.
Используется asyncpg драйвер для PostgreSQL.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Базовый класс для всех моделей"""

    pass


class User(Base):
    """
    Пользователь бота.
    Регистрация через одноразовый токен, привязка по telegram_id.
    """

    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, comment="Telegram ID пользователя"
    )
    active_endpoint_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("endpoints.id", ondelete="SET NULL"),
        nullable=True,
        comment="ID активного эндпоинта",
    )
    active_model: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Активная модель"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        comment="Дата регистрации",
    )

    # Relationships
    endpoints: Mapped[list["Endpoint"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", foreign_keys="Endpoint.user_id"
    )
    favorite_models: Mapped[list["FavoriteModel"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    chat_sessions: Mapped[list["ChatSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    consumed_token: Mapped["RegistrationToken | None"] = relationship(
        back_populates="used_by_user", foreign_keys="RegistrationToken.used_by_telegram_id"
    )


class RegistrationToken(Base):
    """
    Одноразовый токен регистрации.
    Генерируется администратором, используется один раз.
    """

    __tablename__ = "registration_tokens"

    token: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        comment="UUID токена",
    )
    is_used: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="Токен использован?"
    )
    used_by_telegram_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="SET NULL"),
        nullable=True,
        comment="Кто использовал токен",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        comment="Дата создания токена",
    )

    # Relationships
    used_by_user: Mapped[User | None] = relationship(
        back_populates="consumed_token", foreign_keys=[used_by_telegram_id]
    )


class Endpoint(Base):
    """
    OpenAI-совместимый эндпоинт пользователя.
    Каждый пользователь может иметь несколько эндпоинтов.
    """

    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
        comment="Владелец эндпоинта",
    )
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Название эндпоинта"
    )
    base_url: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="URL эндпоинта (без /v1/)"
    )
    api_key_encrypted: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Зашифрованный API ключ"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        comment="Дата добавления",
    )

    # Relationships
    user: Mapped[User] = relationship(back_populates="endpoints", foreign_keys=[user_id])
    favorite_models: Mapped[list["FavoriteModel"]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan"
    )


class FavoriteModel(Base):
    """
    Избранная модель пользователя.
    Быстрый доступ к часто используемым моделям.
    """

    __tablename__ = "favorite_models"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
    )
    endpoint_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("endpoints.id", ondelete="CASCADE"),
        nullable=False,
    )
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Relationships
    user: Mapped[User] = relationship(back_populates="favorite_models")
    endpoint: Mapped[Endpoint] = relationship(back_populates="favorite_models")

    __table_args__ = (
        UniqueConstraint("user_id", "endpoint_id", "model_name", name="uq_user_endpoint_model"),
    )


class ChatSession(Base):
    """
    Сессия чата в топике форума.
    Каждый топик = отдельная сессия с собственной историей.
    Модель и эндпоинт фиксируются за сессией при первом сообщении.
    """

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.telegram_id", ondelete="CASCADE"),
        nullable=False,
    )
    chat_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="ID чата Telegram"
    )
    message_thread_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="ID топика форума"
    )
    model: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="Модель для этой сессии (deprecated, используйте model_name)"
    )
    model_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Зафиксированная модель для этой сессии"
    )
    endpoint_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("endpoints.id", ondelete="SET NULL"),
        nullable=True,
        comment="Зафиксированный эндпоинт для этой сессии",
    )
    pinned_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="ID закреплённого инфо-сообщения о модели"
    )
    topic_renamed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false",
        comment="Флаг: топик переименован по первому сообщению"
    )
    system_prompt: Mapped[str | None] = mapped_column(
        Text, nullable=True, default="You are a helpful assistant.", comment="Системный промпт"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    user: Mapped[User] = relationship(back_populates="chat_sessions")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="Message.created_at"
    )

    __table_args__ = (
        UniqueConstraint("chat_id", "message_thread_id", name="uq_chat_thread"),
        Index("idx_chat_thread", "chat_id", "message_thread_id"),
    )


class Message(Base):
    """
    Сообщение в сессии чата.
    Хранит историю для управления контекстом.
    
    Поддержка мультимодального контента:
    - Если content_parts заполнено — используется оно (формат OpenAI Vision API)
    - Иначе используется поле content (простой текст, обратная совместимость)
    
    Формат content_parts (JSONB массив):
    [
        {"type": "text", "text": "описание изображения"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}
    ]
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("chat_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="system/user/assistant"
    )
    content: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Содержимое сообщения (простой текст)"
    )
    content_parts: Mapped[list | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Мультимодальный контент (текст + изображения) в формате OpenAI",
    )
    token_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Количество токенов"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    session: Mapped[ChatSession] = relationship(back_populates="messages")

    __table_args__ = (Index("idx_session_created", "session_id", "created_at"),)
