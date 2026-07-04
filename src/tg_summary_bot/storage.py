from __future__ import annotations

import asyncio
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


@dataclass(frozen=True)
class StoredImage:
    message_id: int
    chat_id: int
    chat_type: str
    file_id: str
    media_type: str
    sender_name: str
    created_at: str
    file_size: int | None
    file_name: str | None
    mime_type: str | None


@dataclass(frozen=True)
class StoredVideo:
    message_id: int
    chat_id: int
    chat_type: str
    file_id: str
    media_type: str
    sender_name: str
    created_at: str
    duration: int | None
    file_size: int | None
    file_name: str | None
    mime_type: str | None


@dataclass(frozen=True)
class StoredVideoRecognition:
    chat_id: int
    message_id: int
    cache_key: str
    result: str
    created_at: str


@dataclass(frozen=True)
class ChatMemoryBlock:
    block_id: int
    chat_id: int
    period_start: str
    period_end: str
    summary: str
    topics: str
    keywords: str
    message_count: int
    created_at: str


class MessageStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._write_lock = asyncio.Lock()

    async def _prepare_connection(self, db: aiosqlite.Connection) -> None:
        await db.execute("PRAGMA busy_timeout=10000")

    async def init(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS images (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_type TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    sender_id INTEGER,
                    sender_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    file_size INTEGER,
                    file_name TEXT,
                    mime_type TEXT,
                    PRIMARY KEY (chat_id, message_id)
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_images_chat_created
                ON images (chat_id, created_at)
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    chat_type TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    sender_id INTEGER,
                    sender_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    duration INTEGER,
                    file_size INTEGER,
                    file_name TEXT,
                    mime_type TEXT,
                    PRIMARY KEY (chat_id, message_id)
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_videos_chat_created
                ON videos (chat_id, created_at)
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS video_recognitions (
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    cache_key TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, message_id, cache_key)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_memory_blocks (
                    block_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    topics TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    message_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_memory_blocks_chat_period
                ON chat_memory_blocks (chat_id, period_start, period_end)
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_memory_state (
                    chat_id INTEGER PRIMARY KEY,
                    processed_until TEXT NOT NULL
                )
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
        replace: bool = False,
    ) -> None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at_iso = created_at.astimezone(timezone.utc).isoformat()

        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                sql = """
                INSERT OR IGNORE INTO messages (
                    chat_id, message_id, chat_type, sender_id, sender_name,
                    text, created_at, reply_to_message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """
                if replace:
                    sql = """
                    INSERT INTO messages (
                        chat_id, message_id, chat_type, sender_id, sender_name,
                        text, created_at, reply_to_message_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, message_id) DO UPDATE SET
                        chat_type = excluded.chat_type,
                        sender_id = excluded.sender_id,
                        sender_name = excluded.sender_name,
                        text = excluded.text,
                        created_at = excluded.created_at,
                        reply_to_message_id = excluded.reply_to_message_id
                    """
                await db.execute(
                    sql,
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

    async def save_video(
        self,
        *,
        chat_id: int,
        message_id: int,
        chat_type: str,
        file_id: str,
        media_type: str,
        sender_id: int | None,
        sender_name: str,
        created_at: datetime,
        duration: int | None,
        file_size: int | None,
        file_name: str | None,
        mime_type: str | None,
    ) -> None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at_iso = created_at.astimezone(timezone.utc).isoformat()

        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                await db.execute(
                    """
                    INSERT OR REPLACE INTO videos (
                        chat_id, message_id, chat_type, file_id, media_type,
                        sender_id, sender_name, created_at, duration,
                        file_size, file_name, mime_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        message_id,
                        chat_type,
                        file_id,
                        media_type,
                        sender_id,
                        sender_name,
                        created_at_iso,
                        duration,
                        file_size,
                        file_name,
                        mime_type,
                    ),
                )
                await db.commit()

    async def save_image(
        self,
        *,
        chat_id: int,
        message_id: int,
        chat_type: str,
        file_id: str,
        media_type: str,
        sender_id: int | None,
        sender_name: str,
        created_at: datetime,
        file_size: int | None,
        file_name: str | None,
        mime_type: str | None,
    ) -> None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        created_at_iso = created_at.astimezone(timezone.utc).isoformat()

        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                await db.execute(
                    """
                    INSERT OR REPLACE INTO images (
                        chat_id, message_id, chat_type, file_id, media_type,
                        sender_id, sender_name, created_at, file_size, file_name, mime_type
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        message_id,
                        chat_type,
                        file_id,
                        media_type,
                        sender_id,
                        sender_name,
                        created_at_iso,
                        file_size,
                        file_name,
                        mime_type,
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
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    message_id,
                    chat_id,
                    chat_type,
                    sender_name,
                    text,
                    created_at,
                    reply_to_message_id
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

    async def get_memory_message_chunk(
        self,
        *,
        chat_id: int,
        before: datetime,
        after: str | None,
        limit_chars: int,
    ) -> list[StoredMessage]:
        if before.tzinfo is None:
            before = before.replace(tzinfo=timezone.utc)
        before_iso = before.astimezone(timezone.utc).isoformat()

        params: list[object] = [chat_id, before_iso]
        after_clause = ""
        if after:
            after_clause = "AND created_at > ?"
            params.append(after)

        messages: list[StoredMessage] = []
        total_chars = 0
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT
                    message_id,
                    chat_id,
                    chat_type,
                    sender_name,
                    text,
                    created_at,
                    reply_to_message_id
                FROM messages
                WHERE chat_id = ? AND created_at < ? {after_clause}
                ORDER BY created_at ASC, message_id ASC
                """,
                params,
            )
            async for row in cursor:
                text = str(row["text"])
                row_created_at = str(row["created_at"])
                if (
                    total_chars + len(text) > limit_chars
                    and messages
                    and row_created_at != messages[-1].created_at
                ):
                    break
                total_chars += len(text)
                messages.append(_stored_message_from_row(row, text=text))
            await cursor.close()
        return messages

    async def get_latest_image(self, chat_id: int) -> StoredImage | None:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    message_id,
                    chat_id,
                    chat_type,
                    file_id,
                    media_type,
                    sender_name,
                    created_at,
                    file_size,
                    file_name,
                    mime_type
                FROM images
                WHERE chat_id = ?
                ORDER BY created_at DESC, message_id DESC
                LIMIT 1
                """,
                (chat_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return _stored_image_from_row(row) if row else None

    async def get_image_by_message_id(self, chat_id: int, message_id: int) -> StoredImage | None:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    message_id,
                    chat_id,
                    chat_type,
                    file_id,
                    media_type,
                    sender_name,
                    created_at,
                    file_size,
                    file_name,
                    mime_type
                FROM images
                WHERE chat_id = ? AND message_id = ?
                """,
                (chat_id, message_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return _stored_image_from_row(row) if row else None

    async def get_latest_video(self, chat_id: int) -> StoredVideo | None:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    message_id,
                    chat_id,
                    chat_type,
                    file_id,
                    media_type,
                    sender_name,
                    created_at,
                    duration,
                    file_size,
                    file_name,
                    mime_type
                FROM videos
                WHERE chat_id = ?
                ORDER BY created_at DESC, message_id DESC
                LIMIT 1
                """,
                (chat_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return _stored_video_from_row(row) if row else None

    async def get_video_by_message_id(self, chat_id: int, message_id: int) -> StoredVideo | None:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT
                    message_id,
                    chat_id,
                    chat_type,
                    file_id,
                    media_type,
                    sender_name,
                    created_at,
                    duration,
                    file_size,
                    file_name,
                    mime_type
                FROM videos
                WHERE chat_id = ? AND message_id = ?
                """,
                (chat_id, message_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return _stored_video_from_row(row) if row else None

    async def count_messages(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row else 0

    async def count_messages_since(self, *, chat_id: int, since: datetime) -> int:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        since_iso = since.astimezone(timezone.utc).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ? AND created_at >= ?",
                (chat_id, since_iso),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row else 0

    async def count_messages_before_after(
        self,
        *,
        chat_id: int,
        before: datetime,
        after: str | None,
    ) -> int:
        if before.tzinfo is None:
            before = before.replace(tzinfo=timezone.utc)
        before_iso = before.astimezone(timezone.utc).isoformat()
        params: list[object] = [chat_id, before_iso]
        after_clause = ""
        if after:
            after_clause = "AND created_at > ?"
            params.append(after)
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                f"""
                SELECT COUNT(*)
                FROM messages
                WHERE chat_id = ? AND created_at < ? {after_clause}
                """,
                params,
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row else 0

    async def get_memory_processed_until(self, chat_id: int) -> str | None:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                "SELECT processed_until FROM chat_memory_state WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return str(row[0]) if row else None

    async def count_memory_blocks(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM chat_memory_blocks WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row else 0

    async def save_memory_block(
        self,
        *,
        chat_id: int,
        period_start: str,
        period_end: str,
        summary: str,
        topics: str,
        keywords: str,
        message_count: int,
        processed_until: str,
        max_blocks: int,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                await db.execute(
                    """
                    INSERT INTO chat_memory_blocks (
                        chat_id, period_start, period_end, summary, topics,
                        keywords, message_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        period_start,
                        period_end,
                        summary,
                        topics,
                        keywords,
                        message_count,
                        created_at,
                    ),
                )
                await db.execute(
                    """
                    INSERT INTO chat_memory_state (chat_id, processed_until)
                    VALUES (?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET processed_until = excluded.processed_until
                    """,
                    (chat_id, processed_until),
                )
                await db.execute(
                    """
                    DELETE FROM chat_memory_blocks
                    WHERE chat_id = ?
                      AND block_id NOT IN (
                          SELECT block_id
                          FROM chat_memory_blocks
                          WHERE chat_id = ?
                          ORDER BY period_end DESC, block_id DESC
                          LIMIT ?
                      )
                    """,
                    (chat_id, chat_id, max_blocks),
                )
                await db.commit()

    async def get_memory_blocks_for_period(
        self,
        *,
        chat_id: int,
        since: datetime,
        until: datetime,
    ) -> list[ChatMemoryBlock]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        since_iso = since.astimezone(timezone.utc).isoformat()
        until_iso = until.astimezone(timezone.utc).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT block_id, chat_id, period_start, period_end, summary,
                       topics, keywords, message_count, created_at
                FROM chat_memory_blocks
                WHERE chat_id = ? AND period_end >= ? AND period_start <= ?
                ORDER BY period_start ASC, period_end ASC, block_id ASC
                """,
                (chat_id, since_iso, until_iso),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_memory_block_from_row(row) for row in rows]

    async def get_recent_memory_blocks_for_period(
        self,
        *,
        chat_id: int,
        since: datetime,
        until: datetime,
        limit: int,
    ) -> list[ChatMemoryBlock]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        since_iso = since.astimezone(timezone.utc).isoformat()
        until_iso = until.astimezone(timezone.utc).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT block_id, chat_id, period_start, period_end, summary,
                       topics, keywords, message_count, created_at
                FROM chat_memory_blocks
                WHERE chat_id = ? AND period_end >= ? AND period_start <= ?
                ORDER BY period_end DESC, block_id DESC
                LIMIT ?
                """,
                (chat_id, since_iso, until_iso, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_memory_block_from_row(row) for row in rows]

    async def count_images(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM images WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row else 0

    async def count_videos(self, chat_id: int) -> int:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM videos WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row else 0

    async def get_video_recognition(
        self,
        *,
        chat_id: int,
        message_id: int,
        cache_key: str,
    ) -> StoredVideoRecognition | None:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT chat_id, message_id, cache_key, result, created_at
                FROM video_recognitions
                WHERE chat_id = ? AND message_id = ? AND cache_key = ?
                """,
                (chat_id, message_id, cache_key),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return None
        return StoredVideoRecognition(
            chat_id=int(row["chat_id"]),
            message_id=int(row["message_id"]),
            cache_key=str(row["cache_key"]),
            result=str(row["result"]),
            created_at=str(row["created_at"]),
        )

    async def save_video_recognition(
        self,
        *,
        chat_id: int,
        message_id: int,
        cache_key: str,
        result: str,
    ) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                await db.execute(
                    """
                    INSERT OR REPLACE INTO video_recognitions (
                        chat_id, message_id, cache_key, result, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (chat_id, message_id, cache_key, result, created_at),
                )
                await db.commit()


def _stored_image_from_row(row: aiosqlite.Row) -> StoredImage:
    return StoredImage(
        message_id=int(row["message_id"]),
        chat_id=int(row["chat_id"]),
        chat_type=str(row["chat_type"]),
        file_id=str(row["file_id"]),
        media_type=str(row["media_type"]),
        sender_name=str(row["sender_name"]),
        created_at=str(row["created_at"]),
        file_size=row["file_size"],
        file_name=row["file_name"],
        mime_type=row["mime_type"],
    )


def _stored_message_from_row(row: aiosqlite.Row, *, text: str | None = None) -> StoredMessage:
    return StoredMessage(
        message_id=int(row["message_id"]),
        chat_id=int(row["chat_id"]),
        chat_type=str(row["chat_type"]),
        sender_name=str(row["sender_name"]),
        text=str(row["text"]) if text is None else text,
        created_at=str(row["created_at"]),
        reply_to_message_id=row["reply_to_message_id"],
    )


def _memory_block_from_row(row: aiosqlite.Row) -> ChatMemoryBlock:
    return ChatMemoryBlock(
        block_id=int(row["block_id"]),
        chat_id=int(row["chat_id"]),
        period_start=str(row["period_start"]),
        period_end=str(row["period_end"]),
        summary=str(row["summary"]),
        topics=str(row["topics"]),
        keywords=str(row["keywords"]),
        message_count=int(row["message_count"]),
        created_at=str(row["created_at"]),
    )


def _stored_video_from_row(row: aiosqlite.Row) -> StoredVideo:
    return StoredVideo(
        message_id=int(row["message_id"]),
        chat_id=int(row["chat_id"]),
        chat_type=str(row["chat_type"]),
        file_id=str(row["file_id"]),
        media_type=str(row["media_type"]),
        sender_name=str(row["sender_name"]),
        created_at=str(row["created_at"]),
        duration=row["duration"],
        file_size=row["file_size"],
        file_name=row["file_name"],
        mime_type=row["mime_type"],
    )
