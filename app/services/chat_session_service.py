"""Безопасные операции с моделью отдельной сессии форум-топика."""

from dataclasses import dataclass

from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ChatSession, Endpoint


@dataclass(frozen=True)
class OwnedChatSession:
    """Owner-scoped сессия и её endpoint, если он ещё существует."""

    chat_session: ChatSession
    endpoint: Endpoint | None


async def get_owned_chat_session(
    session: AsyncSession,
    *,
    session_id: int,
    user_id: int,
    chat_id: int,
    message_thread_id: int,
) -> OwnedChatSession | None:
    """Загружает owner-scoped сессию; endpoint может быть уже удалён."""
    result = await session.execute(
        select(ChatSession, Endpoint)
        .outerjoin(
            Endpoint,
            (Endpoint.id == ChatSession.endpoint_id)
            & (Endpoint.user_id == ChatSession.user_id),
        )
        .where(
            ChatSession.id == session_id,
            ChatSession.user_id == user_id,
            ChatSession.chat_id == chat_id,
            ChatSession.message_thread_id == message_thread_id,
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    return OwnedChatSession(chat_session=row[0], endpoint=row[1])


async def get_owned_chat_session_by_topic(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    message_thread_id: int,
) -> OwnedChatSession | None:
    """Загружает owner-scoped сессию топика; endpoint может быть уже удалён."""
    result = await session.execute(
        select(ChatSession, Endpoint)
        .outerjoin(
            Endpoint,
            (Endpoint.id == ChatSession.endpoint_id)
            & (Endpoint.user_id == ChatSession.user_id),
        )
        .where(
            ChatSession.user_id == user_id,
            ChatSession.chat_id == chat_id,
            ChatSession.message_thread_id == message_thread_id,
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    return OwnedChatSession(chat_session=row[0], endpoint=row[1])


async def rebind_owned_chat_session(
    session: AsyncSession,
    *,
    session_id: int,
    user_id: int,
    chat_id: int,
    message_thread_id: int,
    expected_endpoint_id: int | None,
    new_endpoint_id: int,
    model_name: str,
) -> bool:
    """CAS-перепривязка endpoint и обоих полей модели одной SQL-операцией.

    Условие включает владельца, координаты Telegram-топика, существование нового
    owner-scoped endpoint и ожидаемый старый endpoint (включая ``NULL``). Поэтому
    callback из устаревшего меню не может перезаписать более свежее переключение.
    Транзакцией и commit/rollback управляет вызывающий код.
    """
    endpoint_owned = exists().where(
        Endpoint.id == new_endpoint_id,
        Endpoint.user_id == user_id,
    )
    expected_endpoint_clause = (
        ChatSession.endpoint_id.is_(None)
        if expected_endpoint_id is None
        else ChatSession.endpoint_id == expected_endpoint_id
    )
    result = await session.execute(
        update(ChatSession)
        .where(
            ChatSession.id == session_id,
            ChatSession.user_id == user_id,
            ChatSession.chat_id == chat_id,
            ChatSession.message_thread_id == message_thread_id,
            expected_endpoint_clause,
            endpoint_owned,
        )
        .values(
            endpoint_id=new_endpoint_id,
            model_name=model_name,
            model=model_name,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    return result.rowcount == 1


async def update_owned_chat_session_model(
    session: AsyncSession,
    *,
    session_id: int,
    user_id: int,
    chat_id: int,
    message_thread_id: int,
    endpoint_id: int,
    model_name: str,
) -> bool:
    """Атомарно обновляет новое и deprecated-поле модели после всех owner-проверок."""
    endpoint_owned = exists().where(
        Endpoint.id == endpoint_id,
        Endpoint.user_id == user_id,
    )
    result = await session.execute(
        update(ChatSession)
        .where(
            ChatSession.id == session_id,
            ChatSession.user_id == user_id,
            ChatSession.chat_id == chat_id,
            ChatSession.message_thread_id == message_thread_id,
            ChatSession.endpoint_id == endpoint_id,
            endpoint_owned,
        )
        .values(model_name=model_name, model=model_name)
        .execution_options(synchronize_session="fetch")
    )
    await session.flush()
    return result.rowcount == 1
