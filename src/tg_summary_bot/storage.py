from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


@dataclass(frozen=True)
class StoredMessage:
    message_id: int
    chat_id: int
    chat_type: str
    sender_name: str
    text: str
    created_at: str
    reply_to_message_id: int | None


class MessageStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    async def init(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_type TEXT NOT NULL,
                    sender_id INTEGER,
                    sender_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reply_to_message_id INTEGER,
                    PRIMARY KEY (chat_id, message_id)
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_chat_created
                ON messages (chat_id, created_at)
                """
            )
            await db.commit()

    async def save_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        chat_type: str,
        sender_id: int | None,
        sender_name: str,
        text: str,
        created_at: datetime,
        reply_to_message_id: int | None,
    ) -> None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at_iso = created_at.astimezone(timezone.utc).isoformat()

        async with aiosqlite.connect(self.database_path) as db:
            await db.execute(
                """
                INSERT OR IGNORE INTO messages (
                    chat_id, message_id, chat_type, sender_id, sender_name,
                    text, created_at, reply_to_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    message_id,
                    chat_type,
                    sender_id,
                    sender_name,
                    text,
                    created_at_iso,
                    reply_to_message_id,
                ),
            )
            await db.commit()

    async def get_messages_since(
        self,
        *,
        chat_id: int,
        since: datetime,
        limit_chars: int,
    ) -> list[StoredMessage]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        since_iso = since.astimezone(timezone.utc).isoformat()

        messages: list[StoredMessage] = []
        total_chars = 0
        async with aiosqlite.connect(self.database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT message_id, chat_id, chat_type, sender_name, text, created_at, reply_to_message_id
                FROM messages
                WHERE chat_id = ? AND created_at >= ?
                ORDER BY created_at ASC, message_id ASC
                """,
                (chat_id, since_iso),
            )
            async for row in cursor:
                text = str(row["text"])
                total_chars += len(text)
                if total_chars > limit_chars and messages:
                    break
                messages.append(
                    StoredMessage(
                        message_id=int(row["message_id"]),
                        chat_id=int(row["chat_id"]),
                        chat_type=str(row["chat_type"]),
                        sender_name=str(row["sender_name"]),
                        text=text,
                        created_at=str(row["created_at"]),
                        reply_to_message_id=row["reply_to_message_id"],
                    )
                )
            await cursor.close()
        return messages

    async def count_messages(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.database_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,))
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row else 0
