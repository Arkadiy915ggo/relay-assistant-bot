from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from tg_summary_bot.casino import CASINO_RULES_VERSION, CASINO_STAKE, slot_result


@dataclass(frozen=True)
class StoredMessage:
    message_id: int
    chat_id: int
    chat_type: str
    sender_id: int | None
    sender_name: str
    text: str
    created_at: str
    reply_to_message_id: int | None
    origin: str = "legacy"
    kind: str = "legacy_unclassified"


ELIGIBLE_MESSAGE_PROVENANCE: frozenset[tuple[str, str]] = frozenset(
    {
        ("incoming", "text"),
        ("incoming", "caption"),
        ("incoming", "voice_transcript"),
        ("assistant", "assistant_answer"),
    }
)


def is_eligible_message_provenance(origin: str, kind: str) -> bool:
    """Return eligibility from the closed Feature 03 provenance allowlist."""
    return (origin, kind) in ELIGIBLE_MESSAGE_PROVENANCE


def eligible_message_provenance_sql(*, table_alias: str = "") -> str:
    prefix = f"{table_alias}." if table_alias else ""
    return (
        f"({prefix}origin = 'incoming' AND {prefix}kind IN ('text', 'caption', 'voice_transcript')) "
        f"OR ({prefix}origin = 'assistant' AND {prefix}kind = 'assistant_answer')"
    )


@dataclass(frozen=True)
class SnapshotBoundary:
    created_at: str
    message_id: int


@dataclass(frozen=True)
class RawSnapshotPage:
    messages: list[StoredMessage]
    next_cursor: SnapshotBoundary | None


@dataclass(frozen=True)
class PointBalance:
    chat_id: int
    participant_key: str
    participant_name: str
    balance: int
    updated_at: str


@dataclass(frozen=True)
class JokeAwardResult:
    status: str
    source_message_id: int | None = None
    participant_key: str | None = None
    participant_name: str | None = None
    source_text: str | None = None
    balance: int | None = None


@dataclass(frozen=True)
class PinnedLeaderboard:
    chat_id: int
    message_id: int
    updated_at: str


@dataclass(frozen=True)
class CasinoSpin:
    spin_id: int
    chat_id: int
    request_message_id: int
    participant_key: str
    participant_name: str
    rules_version: str
    stake: int
    dice_message_id: int | None
    dice_value: int | None
    category: str | None
    payout: int | None
    status: str
    terminal_balance: int | None
    terminal_reason: str | None
    created_at: str
    updated_at: str
    completed_at: str | None
    refunded_at: str | None


@dataclass(frozen=True)
class CasinoSpinResult:
    status: str
    spin: CasinoSpin | None = None


@dataclass(frozen=True)
class CasinoRecoveryResult:
    spin_ids: tuple[int, ...]
    chat_ids: tuple[int, ...]


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
    structured_json: str
    level: str
    created_at: str


@dataclass(frozen=True)
class ChatParticipantFact:
    fact_id: int
    chat_id: int
    participant_key: str
    participant_name: str
    fact_type: str
    fact_text: str
    source_message_ids: str
    confidence: str
    status: str
    first_seen_at: str
    last_seen_at: str
    expires_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChatParticipant:
    sender_id: int | None
    sender_name: str
    last_seen_at: str
    message_count: int


@dataclass(frozen=True)
class ChatBotAlias:
    chat_id: int
    alias: str
    normalized_alias: str
    created_by_user_id: int | None
    created_at: str


class MessageStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._write_lock = asyncio.Lock()
        self._memory_fts_available = False
        self._participant_facts_fts_available = False

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
                    origin TEXT NOT NULL DEFAULT 'legacy',
                    kind TEXT NOT NULL DEFAULT 'legacy_unclassified',
                    PRIMARY KEY (chat_id, message_id)
                )
                """
            )
            await self._ensure_column(
                db,
                table="messages",
                column="origin",
                definition="TEXT NOT NULL DEFAULT 'legacy'",
            )
            await self._ensure_column(
                db,
                table="messages",
                column="kind",
                definition="TEXT NOT NULL DEFAULT 'legacy_unclassified'",
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_chat_created
                ON messages (chat_id, created_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_eligible_snapshot
                ON messages (chat_id, origin, kind, created_at, message_id)
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_point_ledger (
                    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    participant_key TEXT NOT NULL,
                    participant_name TEXT NOT NULL,
                    delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    source_message_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, reason, source_message_id)
                )
                """
            )
            await self._ensure_column(
                db,
                table="chat_point_ledger",
                column="casino_spin_id",
                definition="INTEGER",
            )
            await db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_point_ledger_casino_spin_reason
                ON chat_point_ledger(chat_id, reason, casino_spin_id)
                WHERE casino_spin_id IS NOT NULL
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_point_balances (
                    chat_id INTEGER NOT NULL,
                    participant_key TEXT NOT NULL,
                    participant_name TEXT NOT NULL,
                    balance INTEGER NOT NULL CHECK(balance >= 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, participant_key)
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_casino_spins (
                    spin_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    request_message_id INTEGER NOT NULL CHECK(request_message_id > 0),
                    participant_key TEXT NOT NULL,
                    participant_name TEXT NOT NULL,
                    rules_version TEXT NOT NULL,
                    stake INTEGER NOT NULL CHECK(stake = 10),
                    dice_message_id INTEGER CHECK(dice_message_id > 0),
                    dice_value INTEGER CHECK(dice_value BETWEEN 1 AND 64),
                    category TEXT CHECK(category IN ('none', 'pair', 'triple', 'jackpot')),
                    payout INTEGER CHECK(payout IN (0, 5, 50, 250)),
                    status TEXT NOT NULL CHECK(status IN ('pending', 'completed', 'refunded')),
                    terminal_balance INTEGER CHECK(terminal_balance >= 0),
                    terminal_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    refunded_at TEXT,
                    UNIQUE(chat_id, request_message_id)
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_casino_spins_pending_created
                ON chat_casino_spins(status, created_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_casino_spins_chat_participant
                ON chat_casino_spins(chat_id, participant_key, created_at)
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_pinned_leaderboards (
                    chat_id INTEGER PRIMARY KEY,
                    message_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_bot_aliases (
                    chat_id INTEGER NOT NULL,
                    alias TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL,
                    created_by_user_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, normalized_alias)
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_bot_aliases_chat
                ON chat_bot_aliases (chat_id, normalized_alias)
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
                    structured_json TEXT NOT NULL DEFAULT '{}',
                    level TEXT NOT NULL DEFAULT 'chunk',
                    created_at TEXT NOT NULL
                )
                """
            )
            await self._ensure_column(
                db,
                table="chat_memory_blocks",
                column="structured_json",
                definition="TEXT NOT NULL DEFAULT '{}'",
            )
            await self._ensure_column(
                db,
                table="chat_memory_blocks",
                column="level",
                definition="TEXT NOT NULL DEFAULT 'chunk'",
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_memory_blocks_chat_period
                ON chat_memory_blocks (chat_id, period_start, period_end)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_memory_blocks_chat_level_period
                ON chat_memory_blocks (chat_id, level, period_end)
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
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_profile_state (
                    chat_id INTEGER PRIMARY KEY,
                    processed_until TEXT NOT NULL
                )
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_participant_facts (
                    fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    participant_key TEXT NOT NULL,
                    participant_name TEXT NOT NULL,
                    fact_type TEXT NOT NULL,
                    fact_text TEXT NOT NULL,
                    source_message_ids TEXT NOT NULL DEFAULT '[]',
                    confidence TEXT NOT NULL DEFAULT 'medium',
                    status TEXT NOT NULL DEFAULT 'active',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_id, participant_key, fact_type, fact_text)
                )
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_participant_facts_lookup
                ON chat_participant_facts (chat_id, participant_key, status, last_seen_at)
                """
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_chat_participant_facts_name
                ON chat_participant_facts (chat_id, participant_name, status)
                """
            )
            await self._init_fts_tables(db)
            await db.commit()

    async def _ensure_column(
        self,
        db: aiosqlite.Connection,
        *,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        cursor = await db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        await cursor.close()
        existing = {str(row[1]) for row in rows}
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def _init_fts_tables(self, db: aiosqlite.Connection) -> None:
        try:
            await self._drop_contentless_fts_table(db, "chat_memory_blocks_fts")
            await db.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chat_memory_blocks_fts
                USING fts5(search_text, tokenize='unicode61')
                """
            )
            await db.execute(
                """
                INSERT INTO chat_memory_blocks_fts(rowid, search_text)
                SELECT block_id, summary || ' ' || topics || ' ' || keywords || ' '
                       || structured_json || ' ' || level
                FROM chat_memory_blocks
                WHERE block_id NOT IN (SELECT rowid FROM chat_memory_blocks_fts)
                """
            )
            self._memory_fts_available = True
        except aiosqlite.OperationalError:
            self._memory_fts_available = False

        try:
            await self._drop_contentless_fts_table(db, "chat_participant_facts_fts")
            await db.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS chat_participant_facts_fts
                USING fts5(search_text, tokenize='unicode61')
                """
            )
            await db.execute(
                """
                INSERT INTO chat_participant_facts_fts(rowid, search_text)
                SELECT fact_id, participant_name || ' ' || fact_type || ' ' || fact_text
                       || ' ' || confidence || ' ' || status
                FROM chat_participant_facts
                WHERE fact_id NOT IN (SELECT rowid FROM chat_participant_facts_fts)
                """
            )
            self._participant_facts_fts_available = True
        except aiosqlite.OperationalError:
            self._participant_facts_fts_available = False

    async def _drop_contentless_fts_table(self, db: aiosqlite.Connection, table: str) -> None:
        cursor = await db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if not row:
            return
        schema = str(row[0] or "").replace(" ", "").lower()
        if "content=''" in schema or 'content=""' in schema:
            await db.execute(f"DROP TABLE IF EXISTS {table}")

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
        origin: str = "legacy",
        kind: str = "legacy_unclassified",
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
                        text, created_at, reply_to_message_id, origin, kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
                if replace:
                    sql = """
                    INSERT INTO messages (
                        chat_id, message_id, chat_type, sender_id, sender_name,
                        text, created_at, reply_to_message_id, origin, kind
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, message_id) DO UPDATE SET
                        chat_type = excluded.chat_type,
                        sender_id = excluded.sender_id,
                        sender_name = excluded.sender_name,
                        text = excluded.text,
                        created_at = excluded.created_at,
                        reply_to_message_id = excluded.reply_to_message_id,
                        origin = excluded.origin,
                        kind = excluded.kind
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
                        origin,
                        kind,
                    ),
                )
                await db.commit()

    async def add_chat_bot_alias(
        self,
        *,
        chat_id: int,
        alias: str,
        normalized_alias: str,
        created_by_user_id: int | None,
        created_at: datetime,
    ) -> bool:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                cursor = await db.execute(
                    """
                    INSERT OR IGNORE INTO chat_bot_aliases (
                        chat_id, alias, normalized_alias, created_by_user_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        alias,
                        normalized_alias,
                        created_by_user_id,
                        created_at.astimezone(timezone.utc).isoformat(),
                    ),
                )
                inserted = cursor.rowcount > 0
                await cursor.close()
                await db.commit()
        return inserted

    async def remove_chat_bot_alias(self, *, chat_id: int, normalized_alias: str) -> bool:
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                cursor = await db.execute(
                    "DELETE FROM chat_bot_aliases WHERE chat_id = ? AND normalized_alias = ?",
                    (chat_id, normalized_alias),
                )
                deleted = cursor.rowcount > 0
                await cursor.close()
                await db.commit()
        return deleted

    async def get_chat_bot_aliases(self, chat_id: int) -> list[ChatBotAlias]:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT chat_id, alias, normalized_alias, created_by_user_id, created_at
                FROM chat_bot_aliases
                WHERE chat_id = ?
                ORDER BY created_at ASC, normalized_alias ASC
                """,
                (chat_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [
            ChatBotAlias(
                chat_id=int(row["chat_id"]),
                alias=str(row["alias"]),
                normalized_alias=str(row["normalized_alias"]),
                created_by_user_id=(
                    int(row["created_by_user_id"])
                    if row["created_by_user_id"] is not None
                    else None
                ),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

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
                    sender_id,
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
                        sender_id=row["sender_id"],
                        sender_name=str(row["sender_name"]),
                        text=text,
                        created_at=str(row["created_at"]),
                        reply_to_message_id=row["reply_to_message_id"],
                    )
                )
            await cursor.close()
        return messages

    async def get_eligible_snapshot_boundary(
        self,
        *,
        chat_id: int,
        since: datetime,
    ) -> SnapshotBoundary | None:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        since_iso = since.astimezone(timezone.utc).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                f"""
                SELECT created_at, message_id
                FROM messages
                WHERE chat_id = ? AND created_at >= ?
                  AND ({eligible_message_provenance_sql()})
                ORDER BY created_at DESC, message_id DESC
                LIMIT 1
                """,
                (chat_id, since_iso),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return None
        return SnapshotBoundary(created_at=str(row[0]), message_id=int(row[1]))

    async def get_eligible_snapshot_page(
        self,
        *,
        chat_id: int,
        since: datetime,
        boundary: SnapshotBoundary,
        after: SnapshotBoundary | None,
        row_limit: int,
        char_budget: int,
    ) -> RawSnapshotPage:
        if row_limit <= 0 or char_budget <= 0:
            raise ValueError("row_limit and char_budget must be positive")
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        params: list[object] = [
            chat_id,
            since.astimezone(timezone.utc).isoformat(),
            boundary.created_at,
            boundary.created_at,
            boundary.message_id,
        ]
        after_clause = ""
        if after:
            after_clause = (
                "AND (created_at > ? OR (created_at = ? AND message_id > ?))"
            )
            params.extend([after.created_at, after.created_at, after.message_id])
        params.append(row_limit)
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT message_id, chat_id, chat_type, sender_id, sender_name, text,
                       created_at, reply_to_message_id, origin, kind
                FROM messages
                WHERE chat_id = ? AND created_at >= ?
                  AND (created_at < ? OR (created_at = ? AND message_id <= ?))
                  {after_clause}
                  AND ({eligible_message_provenance_sql()})
                ORDER BY created_at ASC, message_id ASC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
            await cursor.close()

        messages: list[StoredMessage] = []
        total_chars = 0
        for row in rows:
            text = str(row["text"])
            # An oversized first row forms its own page so the keyset cursor advances.
            if messages and total_chars + len(text) > char_budget:
                break
            messages.append(_stored_message_from_row(row, text=text))
            total_chars += len(text)
        if not messages:
            return RawSnapshotPage(messages=[], next_cursor=None)
        last = messages[-1]
        return RawSnapshotPage(
            messages=messages,
            next_cursor=SnapshotBoundary(created_at=last.created_at, message_id=last.message_id),
        )

    async def award_unique_joke(
        self,
        *,
        chat_id: int,
        source_message_id: int,
        awarded_at: datetime | None = None,
    ) -> JokeAwardResult:
        awarded_at = awarded_at or datetime.now(timezone.utc)
        if awarded_at.tzinfo is None:
            awarded_at = awarded_at.replace(tzinfo=timezone.utc)
        awarded_at_iso = awarded_at.astimezone(timezone.utc).isoformat()
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                db.row_factory = aiosqlite.Row
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    cursor = await db.execute(
                        """
                        SELECT message_id, chat_id, sender_id, sender_name, text, origin, kind
                        FROM messages WHERE chat_id = ? AND message_id = ?
                        """,
                        (chat_id, source_message_id),
                    )
                    source = await cursor.fetchone()
                    await cursor.close()
                    if not source:
                        await db.rollback()
                        return JokeAwardResult(status="not_found")
                    origin = str(source["origin"])
                    kind = str(source["kind"])
                    if not is_eligible_message_provenance(origin, kind):
                        await db.rollback()
                        return JokeAwardResult(status="ineligible")

                    sender_id = source["sender_id"]
                    name = str(source["sender_name"])
                    source_text = str(source["text"])
                    key = _participant_key(int(sender_id) if sender_id is not None else None, name)
                    cursor = await db.execute(
                        """
                        INSERT INTO chat_point_ledger (
                            chat_id, participant_key, participant_name, delta, reason,
                            source_message_id, created_at
                        ) VALUES (?, ?, ?, 10, 'best_joke', ?, ?)
                        ON CONFLICT(chat_id, reason, source_message_id) DO NOTHING
                        """,
                        (chat_id, key, name, source_message_id, awarded_at_iso),
                    )
                    inserted = cursor.rowcount > 0
                    await cursor.close()
                    if not inserted:
                        cursor = await db.execute(
                            """
                            SELECT balance FROM chat_point_balances
                            WHERE chat_id = ? AND participant_key = ?
                            """,
                            (chat_id, key),
                        )
                        balance_row = await cursor.fetchone()
                        await cursor.close()
                        await db.rollback()
                        return JokeAwardResult(
                            status="already_awarded",
                            source_message_id=int(source["message_id"]),
                            participant_key=key,
                            participant_name=name,
                            source_text=source_text,
                            balance=int(balance_row[0]) if balance_row else 0,
                        )
                    await db.execute(
                        """
                        INSERT INTO chat_point_balances (
                            chat_id, participant_key, participant_name, balance, updated_at
                        ) VALUES (?, ?, ?, 10, ?)
                        ON CONFLICT(chat_id, participant_key) DO UPDATE SET
                            participant_name = excluded.participant_name,
                            balance = chat_point_balances.balance + 10,
                            updated_at = excluded.updated_at
                        """,
                        (chat_id, key, name, awarded_at_iso),
                    )
                    cursor = await db.execute(
                        """
                        SELECT balance FROM chat_point_balances
                        WHERE chat_id = ? AND participant_key = ?
                        """,
                        (chat_id, key),
                    )
                    balance_row = await cursor.fetchone()
                    await cursor.close()
                    await db.commit()
                    return JokeAwardResult(
                        status="awarded",
                        source_message_id=int(source["message_id"]),
                        participant_key=key,
                        participant_name=name,
                        source_text=source_text,
                        balance=int(balance_row[0]),
                    )
                except Exception:
                    await db.rollback()
                    raise

    async def reserve_casino_spin(
        self,
        *,
        chat_id: int,
        request_message_id: int,
        participant_key: str,
        participant_name: str,
        created_at: datetime | None = None,
    ) -> CasinoSpinResult:
        if request_message_id <= 0:
            raise ValueError("request_message_id must be positive")
        created_at = created_at or datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        now = created_at.astimezone(timezone.utc).isoformat()
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                db.row_factory = aiosqlite.Row
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    cursor = await db.execute(
                        "SELECT * FROM chat_casino_spins WHERE chat_id = ? AND request_message_id = ?",
                        (chat_id, request_message_id),
                    )
                    existing = await cursor.fetchone()
                    await cursor.close()
                    if existing:
                        spin = _casino_spin_from_row(existing)
                        await db.rollback()
                        if spin.participant_key != participant_key:
                            return CasinoSpinResult("identity_conflict", spin)
                        return CasinoSpinResult(f"existing_{spin.status}", spin)

                    cursor = await db.execute(
                        """
                        INSERT INTO chat_casino_spins (
                            chat_id, request_message_id, participant_key, participant_name,
                            rules_version, stake, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            chat_id,
                            request_message_id,
                            participant_key,
                            participant_name,
                            CASINO_RULES_VERSION,
                            CASINO_STAKE,
                            now,
                            now,
                        ),
                    )
                    spin_id = int(cursor.lastrowid)
                    await cursor.close()
                    cursor = await db.execute(
                        """
                        UPDATE chat_point_balances
                        SET balance = balance - ?, participant_name = ?, updated_at = ?
                        WHERE chat_id = ? AND participant_key = ? AND balance >= ?
                        """,
                        (CASINO_STAKE, participant_name, now, chat_id, participant_key, CASINO_STAKE),
                    )
                    debited = cursor.rowcount > 0
                    await cursor.close()
                    if not debited:
                        await db.rollback()
                        return CasinoSpinResult("insufficient_balance")
                    await db.execute(
                        """
                        INSERT INTO chat_point_ledger (
                            chat_id, participant_key, participant_name, delta, reason,
                            source_message_id, casino_spin_id, created_at
                        ) VALUES (?, ?, ?, ?, 'casino_bet', ?, ?, ?)
                        """,
                        (
                            chat_id,
                            participant_key,
                            participant_name,
                            -CASINO_STAKE,
                            request_message_id,
                            spin_id,
                            now,
                        ),
                    )
                    spin = await _get_casino_spin(db, spin_id)
                    await db.commit()
                    return CasinoSpinResult("created", spin)
                except Exception:
                    await db.rollback()
                    raise

    async def settle_casino_spin(
        self,
        *,
        spin_id: int,
        dice_message_id: int,
        dice_value: int,
        completed_at: datetime | None = None,
    ) -> CasinoSpinResult:
        if type(dice_message_id) is not int or dice_message_id <= 0:
            return CasinoSpinResult("invalid_dice")
        try:
            outcome = slot_result(dice_value)
        except ValueError:
            return CasinoSpinResult("invalid_dice")
        completed_at = completed_at or datetime.now(timezone.utc)
        if completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=timezone.utc)
        now = completed_at.astimezone(timezone.utc).isoformat()
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                db.row_factory = aiosqlite.Row
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    spin = await _get_casino_spin(db, spin_id)
                    if not spin:
                        await db.rollback()
                        return CasinoSpinResult("not_found")
                    if spin.status == "completed":
                        await db.rollback()
                        return CasinoSpinResult("already_completed", spin)
                    if spin.status == "refunded":
                        await db.rollback()
                        return CasinoSpinResult("refunded_conflict", spin)
                    await db.execute(
                        """
                        INSERT INTO chat_point_ledger (
                            chat_id, participant_key, participant_name, delta, reason,
                            source_message_id, casino_spin_id, created_at
                        ) VALUES (?, ?, ?, ?, 'casino_payout', ?, ?, ?)
                        """,
                        (
                            spin.chat_id,
                            spin.participant_key,
                            spin.participant_name,
                            outcome.payout,
                            spin.request_message_id,
                            spin.spin_id,
                            now,
                        ),
                    )
                    await db.execute(
                        """
                        UPDATE chat_point_balances
                        SET balance = balance + ?, participant_name = ?, updated_at = ?
                        WHERE chat_id = ? AND participant_key = ?
                        """,
                        (outcome.payout, spin.participant_name, now, spin.chat_id, spin.participant_key),
                    )
                    cursor = await db.execute(
                        "SELECT balance FROM chat_point_balances WHERE chat_id = ? AND participant_key = ?",
                        (spin.chat_id, spin.participant_key),
                    )
                    balance_row = await cursor.fetchone()
                    await cursor.close()
                    if not balance_row:
                        raise RuntimeError("casino balance disappeared during settlement")
                    cursor = await db.execute(
                        """
                        UPDATE chat_casino_spins
                        SET dice_message_id = ?, dice_value = ?, category = ?, payout = ?,
                            status = 'completed', terminal_balance = ?, updated_at = ?, completed_at = ?
                        WHERE spin_id = ? AND status = 'pending'
                        """,
                        (
                            dice_message_id,
                            dice_value,
                            outcome.category,
                            outcome.payout,
                            int(balance_row[0]),
                            now,
                            now,
                            spin_id,
                        ),
                    )
                    transitioned = cursor.rowcount > 0
                    await cursor.close()
                    if not transitioned:
                        raise RuntimeError("casino settlement transition failed")
                    settled = await _get_casino_spin(db, spin_id)
                    await db.commit()
                    return CasinoSpinResult("completed", settled)
                except Exception:
                    await db.rollback()
                    raise

    async def refund_casino_spin(
        self,
        *,
        spin_id: int,
        reason: str,
        refunded_at: datetime | None = None,
    ) -> CasinoSpinResult:
        if not reason or len(reason) > 100:
            raise ValueError("refund reason must be a short stable code")
        refunded_at = refunded_at or datetime.now(timezone.utc)
        if refunded_at.tzinfo is None:
            refunded_at = refunded_at.replace(tzinfo=timezone.utc)
        now = refunded_at.astimezone(timezone.utc).isoformat()
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                db.row_factory = aiosqlite.Row
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    spin = await _get_casino_spin(db, spin_id)
                    if not spin:
                        await db.rollback()
                        return CasinoSpinResult("not_found")
                    if spin.status == "refunded":
                        await db.rollback()
                        return CasinoSpinResult("already_refunded", spin)
                    if spin.status == "completed":
                        await db.rollback()
                        return CasinoSpinResult("completed_conflict", spin)
                    await db.execute(
                        """
                        INSERT INTO chat_point_ledger (
                            chat_id, participant_key, participant_name, delta, reason,
                            source_message_id, casino_spin_id, created_at
                        ) VALUES (?, ?, ?, ?, 'casino_refund', ?, ?, ?)
                        """,
                        (
                            spin.chat_id,
                            spin.participant_key,
                            spin.participant_name,
                            spin.stake,
                            spin.request_message_id,
                            spin.spin_id,
                            now,
                        ),
                    )
                    await db.execute(
                        """
                        UPDATE chat_point_balances
                        SET balance = balance + ?, participant_name = ?, updated_at = ?
                        WHERE chat_id = ? AND participant_key = ?
                        """,
                        (spin.stake, spin.participant_name, now, spin.chat_id, spin.participant_key),
                    )
                    cursor = await db.execute(
                        "SELECT balance FROM chat_point_balances WHERE chat_id = ? AND participant_key = ?",
                        (spin.chat_id, spin.participant_key),
                    )
                    balance_row = await cursor.fetchone()
                    await cursor.close()
                    if not balance_row:
                        raise RuntimeError("casino balance disappeared during refund")
                    cursor = await db.execute(
                        """
                        UPDATE chat_casino_spins
                        SET status = 'refunded', terminal_balance = ?, terminal_reason = ?,
                            updated_at = ?, refunded_at = ?
                        WHERE spin_id = ? AND status = 'pending'
                        """,
                        (int(balance_row[0]), reason, now, now, spin_id),
                    )
                    transitioned = cursor.rowcount > 0
                    await cursor.close()
                    if not transitioned:
                        raise RuntimeError("casino refund transition failed")
                    refunded = await _get_casino_spin(db, spin_id)
                    await db.commit()
                    return CasinoSpinResult("refunded", refunded)
                except Exception:
                    await db.rollback()
                    raise

    async def refund_pending_casino_spins(
        self,
        *,
        created_before: datetime,
    ) -> CasinoRecoveryResult:
        if created_before.tzinfo is None:
            created_before = created_before.replace(tzinfo=timezone.utc)
        cutoff = created_before.astimezone(timezone.utc).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                """
                SELECT spin_id FROM chat_casino_spins
                WHERE status = 'pending' AND created_at < ? ORDER BY spin_id ASC
                """,
                (cutoff,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        spin_ids: list[int] = []
        chat_ids: set[int] = set()
        for row in rows:
            result = await self.refund_casino_spin(
                spin_id=int(row[0]),
                reason="startup_recovery",
                refunded_at=created_before,
            )
            if result.status == "refunded" and result.spin:
                spin_ids.append(result.spin.spin_id)
                chat_ids.add(result.spin.chat_id)
        return CasinoRecoveryResult(tuple(spin_ids), tuple(sorted(chat_ids)))

    async def get_point_balance(self, *, chat_id: int, participant_key: str) -> PointBalance | None:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT chat_id, participant_key, participant_name, balance, updated_at
                FROM chat_point_balances WHERE chat_id = ? AND participant_key = ?
                """,
                (chat_id, participant_key),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return _point_balance_from_row(row) if row else None

    async def get_top_point_balances(self, *, chat_id: int, limit: int = 10) -> list[PointBalance]:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT chat_id, participant_key, participant_name, balance, updated_at
                FROM chat_point_balances
                WHERE chat_id = ? AND balance > 0
                ORDER BY balance DESC, participant_key ASC
                """,
                (chat_id,),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        balances = [_point_balance_from_row(row) for row in rows]
        balances.sort(
            key=lambda item: (
                -item.balance,
                _normalized_participant_name(item.participant_name),
                item.participant_key,
            )
        )
        return balances[: max(0, limit)]

    async def get_pinned_leaderboard(self, *, chat_id: int) -> PinnedLeaderboard | None:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT chat_id, message_id, updated_at
                FROM chat_pinned_leaderboards WHERE chat_id = ?
                """,
                (chat_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return None
        return PinnedLeaderboard(
            chat_id=int(row["chat_id"]),
            message_id=int(row["message_id"]),
            updated_at=str(row["updated_at"]),
        )

    async def save_pinned_leaderboard(self, *, chat_id: int, message_id: int) -> None:
        updated_at = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                await db.execute(
                    """
                    INSERT INTO chat_pinned_leaderboards (chat_id, message_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        message_id = excluded.message_id,
                        updated_at = excluded.updated_at
                    """,
                    (chat_id, message_id, updated_at),
                )
                await db.commit()

    async def delete_pinned_leaderboard(self, *, chat_id: int) -> None:
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                await db.execute("DELETE FROM chat_pinned_leaderboards WHERE chat_id = ?", (chat_id,))
                await db.commit()

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
                    sender_id,
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

    async def get_profile_message_chunk(
        self,
        *,
        chat_id: int,
        after: str,
        before: datetime,
        limit_chars: int,
    ) -> list[StoredMessage]:
        if before.tzinfo is None:
            before = before.replace(tzinfo=timezone.utc)
        before_iso = before.astimezone(timezone.utc).isoformat()

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
                    sender_id,
                    sender_name,
                    text,
                    created_at,
                    reply_to_message_id
                FROM messages
                WHERE chat_id = ? AND created_at > ? AND created_at <= ?
                ORDER BY created_at ASC, message_id ASC
                """,
                (chat_id, after, before_iso),
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

    async def get_chat_participants(
        self,
        *,
        chat_id: int,
        limit: int = 200,
    ) -> list[ChatParticipant]:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT sender_id, sender_name, MAX(created_at) AS last_seen_at,
                       COUNT(*) AS message_count
                FROM messages
                WHERE chat_id = ?
                GROUP BY sender_id, sender_name
                ORDER BY last_seen_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()

        participants: list[ChatParticipant] = []
        for row in rows:
            sender_id = row["sender_id"]
            participants.append(
                ChatParticipant(
                    sender_id=int(sender_id) if sender_id is not None else None,
                    sender_name=str(row["sender_name"]),
                    last_seen_at=str(row["last_seen_at"]),
                    message_count=int(row["message_count"] or 0),
                )
            )
        return participants

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

    async def get_profile_processed_until(self, chat_id: int) -> str | None:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                "SELECT processed_until FROM chat_profile_state WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        return str(row[0]) if row else None

    async def save_profile_processed_until(self, chat_id: int, processed_until: str) -> None:
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                await db.execute(
                    """
                    INSERT INTO chat_profile_state (chat_id, processed_until)
                    VALUES (?, ?)
                    ON CONFLICT(chat_id) DO UPDATE SET
                        processed_until = excluded.processed_until
                    """,
                    (chat_id, processed_until),
                )
                await db.commit()

    async def count_memory_blocks(self, chat_id: int, *, level: str | None = None) -> int:
        params: list[object] = [chat_id]
        level_clause = ""
        if level:
            level_clause = " AND level = ?"
            params.append(level)
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM chat_memory_blocks WHERE chat_id = ?{level_clause}",
                params,
            )
            row = await cursor.fetchone()
            await cursor.close()
        return int(row[0]) if row else 0

    async def count_participant_facts(self, chat_id: int, *, active_only: bool = True) -> int:
        params: list[object] = [chat_id]
        status_clause = ""
        if active_only:
            status_clause = " AND status = 'active'"
        now_iso = datetime.now(timezone.utc).isoformat()
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            cursor = await db.execute(
                f"""
                SELECT COUNT(*)
                FROM chat_participant_facts
                WHERE chat_id = ? {status_clause}
                  AND (expires_at IS NULL OR expires_at >= ?)
                """,
                [*params, now_iso],
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
        structured_json: str,
        level: str,
        processed_until: str | None,
    ) -> int:
        created_at = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                cursor = await db.execute(
                    """
                    INSERT INTO chat_memory_blocks (
                        chat_id, period_start, period_end, summary, topics,
                        keywords, message_count, structured_json, level, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        period_start,
                        period_end,
                        summary,
                        topics,
                        keywords,
                        message_count,
                        structured_json,
                        level,
                        created_at,
                    ),
                )
                block_id = int(cursor.lastrowid)
                await cursor.close()
                if processed_until:
                    await db.execute(
                        """
                        INSERT INTO chat_memory_state (chat_id, processed_until)
                        VALUES (?, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET processed_until = excluded.processed_until
                        """,
                        (chat_id, processed_until),
                    )
                if self._memory_fts_available:
                    await db.execute(
                        """
                        INSERT INTO chat_memory_blocks_fts(rowid, search_text)
                        VALUES (?, ?)
                        """,
                        (
                            block_id,
                            _memory_block_search_text(
                                summary=summary,
                                topics=topics,
                                keywords=keywords,
                                structured_json=structured_json,
                                level=level,
                            ),
                        ),
                    )
                await db.commit()
        return block_id

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
                       topics, keywords, message_count, structured_json,
                       level, created_at
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
                       topics, keywords, message_count, structured_json,
                       level, created_at
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

    async def get_memory_blocks_by_ids(self, block_ids: list[int]) -> list[ChatMemoryBlock]:
        if not block_ids:
            return []
        placeholders = ",".join("?" for _ in block_ids)
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT block_id, chat_id, period_start, period_end, summary,
                       topics, keywords, message_count, structured_json,
                       level, created_at
                FROM chat_memory_blocks
                WHERE block_id IN ({placeholders})
                """,
                block_ids,
            )
            rows = await cursor.fetchall()
            await cursor.close()
        blocks = [_memory_block_from_row(row) for row in rows]
        by_id = {block.block_id: block for block in blocks}
        return [by_id[block_id] for block_id in block_ids if block_id in by_id]

    async def search_memory_blocks(
        self,
        *,
        chat_id: int,
        since: datetime,
        until: datetime,
        query: str,
        limit: int,
    ) -> list[ChatMemoryBlock]:
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        since_iso = since.astimezone(timezone.utc).isoformat()
        until_iso = until.astimezone(timezone.utc).isoformat()

        terms = _search_terms(query)
        if not terms:
            return await self.get_recent_memory_blocks_for_period(
                chat_id=chat_id,
                since=since,
                until=until,
                limit=limit,
            )

        if self._memory_fts_available:
            fts_query = " OR ".join(terms)
            try:
                async with aiosqlite.connect(self.database_path) as db:
                    await self._prepare_connection(db)
                    cursor = await db.execute(
                        """
                        SELECT b.block_id
                        FROM chat_memory_blocks_fts
                        JOIN chat_memory_blocks b ON b.block_id = chat_memory_blocks_fts.rowid
                        WHERE chat_memory_blocks_fts.search_text MATCH ?
                          AND b.chat_id = ?
                          AND b.period_end >= ?
                          AND b.period_start <= ?
                        ORDER BY bm25(chat_memory_blocks_fts), b.period_end DESC
                        LIMIT ?
                        """,
                        (fts_query, chat_id, since_iso, until_iso, limit),
                    )
                    rows = await cursor.fetchall()
                    await cursor.close()
                return await self.get_memory_blocks_by_ids([int(row[0]) for row in rows])
            except aiosqlite.OperationalError:
                self._memory_fts_available = False

        like_clauses = []
        params: list[object] = [chat_id, since_iso, until_iso]
        for term in terms:
            like_clauses.append(
                "(lower(summary) LIKE ? OR lower(topics) LIKE ? OR "
                "lower(keywords) LIKE ? OR lower(structured_json) LIKE ?)"
            )
            pattern = f"%{term.lower()}%"
            params.extend([pattern, pattern, pattern, pattern])
        params.append(limit)
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT block_id, chat_id, period_start, period_end, summary,
                       topics, keywords, message_count, structured_json,
                       level, created_at
                FROM chat_memory_blocks
                WHERE chat_id = ? AND period_end >= ? AND period_start <= ?
                  AND ({' OR '.join(like_clauses)})
                ORDER BY period_end DESC, block_id DESC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_memory_block_from_row(row) for row in rows]

    async def get_oldest_memory_blocks(
        self,
        *,
        chat_id: int,
        level: str,
        limit: int,
    ) -> list[ChatMemoryBlock]:
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT block_id, chat_id, period_start, period_end, summary,
                       topics, keywords, message_count, structured_json,
                       level, created_at
                FROM chat_memory_blocks
                WHERE chat_id = ? AND level = ?
                ORDER BY period_start ASC, period_end ASC, block_id ASC
                LIMIT ?
                """,
                (chat_id, level, limit),
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_memory_block_from_row(row) for row in rows]

    async def delete_memory_blocks(self, block_ids: list[int]) -> None:
        if not block_ids:
            return
        placeholders = ",".join("?" for _ in block_ids)
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                if self._memory_fts_available:
                    await db.execute(
                        f"DELETE FROM chat_memory_blocks_fts WHERE rowid IN ({placeholders})",
                        block_ids,
                    )
                await db.execute(
                    f"DELETE FROM chat_memory_blocks WHERE block_id IN ({placeholders})",
                    block_ids,
                )
                await db.commit()

    async def reset_chat_memory(self, chat_id: int) -> None:
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                if self._memory_fts_available:
                    await db.execute(
                        """
                        DELETE FROM chat_memory_blocks_fts
                        WHERE rowid IN (
                            SELECT block_id FROM chat_memory_blocks WHERE chat_id = ?
                        )
                        """,
                        (chat_id,),
                    )
                await db.execute("DELETE FROM chat_memory_blocks WHERE chat_id = ?", (chat_id,))
                await db.execute("DELETE FROM chat_memory_state WHERE chat_id = ?", (chat_id,))
                await db.commit()

    async def save_participant_fact(
        self,
        *,
        chat_id: int,
        participant_key: str,
        participant_name: str,
        fact_type: str,
        fact_text: str,
        source_message_ids: list[int],
        confidence: str,
        first_seen_at: str,
        last_seen_at: str,
        expires_at: str | None,
        status: str = "active",
    ) -> int:
        now_iso = datetime.now(timezone.utc).isoformat()
        confidence = _best_confidence(confidence, "low")
        source_ids_json = json.dumps(sorted(set(source_message_ids)), ensure_ascii=False)
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    SELECT fact_id, source_message_ids, confidence, first_seen_at
                    FROM chat_participant_facts
                    WHERE chat_id = ? AND participant_key = ?
                      AND fact_type = ? AND fact_text = ?
                    """,
                    (chat_id, participant_key, fact_type, fact_text),
                )
                existing = await cursor.fetchone()
                await cursor.close()
                if existing:
                    fact_id = int(existing["fact_id"])
                    merged_ids = _merge_message_ids(
                        str(existing["source_message_ids"]),
                        source_message_ids,
                    )
                    merged_confidence = _best_confidence(
                        str(existing["confidence"]),
                        confidence,
                    )
                    indexed_confidence = merged_confidence
                    first_seen = str(existing["first_seen_at"] or first_seen_at)
                    await db.execute(
                        """
                        UPDATE chat_participant_facts
                        SET participant_name = ?, source_message_ids = ?,
                            confidence = ?, status = ?, first_seen_at = ?,
                            last_seen_at = ?, expires_at = ?, updated_at = ?
                        WHERE fact_id = ?
                        """,
                        (
                            participant_name,
                            json.dumps(merged_ids, ensure_ascii=False),
                            merged_confidence,
                            status,
                            first_seen,
                            last_seen_at,
                            expires_at,
                            now_iso,
                            fact_id,
                        ),
                    )
                else:
                    indexed_confidence = confidence
                    cursor = await db.execute(
                        """
                        INSERT INTO chat_participant_facts (
                            chat_id, participant_key, participant_name, fact_type,
                            fact_text, source_message_ids, confidence, status,
                            first_seen_at, last_seen_at, expires_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chat_id,
                            participant_key,
                            participant_name,
                            fact_type,
                            fact_text,
                            source_ids_json,
                            confidence,
                            status,
                            first_seen_at,
                            last_seen_at,
                            expires_at,
                            now_iso,
                            now_iso,
                        ),
                    )
                    fact_id = int(cursor.lastrowid)
                    await cursor.close()

                if self._participant_facts_fts_available:
                    await db.execute(
                        "DELETE FROM chat_participant_facts_fts WHERE rowid = ?",
                        (fact_id,),
                    )
                    await db.execute(
                        """
                        INSERT INTO chat_participant_facts_fts(rowid, search_text)
                        VALUES (?, ?)
                        """,
                        (
                            fact_id,
                            _participant_fact_search_text(
                                participant_name=participant_name,
                                fact_type=fact_type,
                                fact_text=fact_text,
                                confidence=indexed_confidence,
                                status=status,
                            ),
                        ),
                    )
                await db.commit()
        return fact_id

    async def get_participant_facts(
        self,
        *,
        chat_id: int,
        participant_keys: list[str] | None = None,
        participant_name: str | None = None,
        include_inactive: bool = False,
        limit: int = 20,
    ) -> list[ChatParticipantFact]:
        clauses = ["chat_id = ?"]
        params: list[object] = [chat_id]
        if participant_keys:
            placeholders = ",".join("?" for _ in participant_keys)
            clauses.append(f"participant_key IN ({placeholders})")
            params.extend(participant_keys)
        if participant_name:
            pattern = f"%{participant_name.strip().lower()}%"
            clauses.append("lower(participant_name) LIKE ?")
            params.append(pattern)
        if not include_inactive:
            clauses.append("status = 'active'")
            clauses.append("(expires_at IS NULL OR expires_at >= ?)")
            params.append(datetime.now(timezone.utc).isoformat())
        params.append(limit)
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT fact_id, chat_id, participant_key, participant_name,
                       fact_type, fact_text, source_message_ids, confidence,
                       status, first_seen_at, last_seen_at, expires_at,
                       created_at, updated_at
                FROM chat_participant_facts
                WHERE {' AND '.join(clauses)}
                ORDER BY last_seen_at DESC, fact_id DESC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_participant_fact_from_row(row) for row in rows]

    async def search_participant_facts(
        self,
        *,
        chat_id: int,
        query: str,
        participant_keys: list[str] | None = None,
        limit: int = 20,
    ) -> list[ChatParticipantFact]:
        terms = _search_terms(query)
        now_iso = datetime.now(timezone.utc).isoformat()
        key_clause = ""
        key_params: list[object] = []
        if participant_keys:
            placeholders = ",".join("?" for _ in participant_keys)
            key_clause = f" AND p.participant_key IN ({placeholders})"
            key_params.extend(participant_keys)

        if terms and self._participant_facts_fts_available:
            fts_query = " OR ".join(terms)
            try:
                async with aiosqlite.connect(self.database_path) as db:
                    await self._prepare_connection(db)
                    db.row_factory = aiosqlite.Row
                    cursor = await db.execute(
                        f"""
                        SELECT p.fact_id, p.chat_id, p.participant_key,
                               p.participant_name, p.fact_type, p.fact_text,
                               p.source_message_ids, p.confidence, p.status,
                               p.first_seen_at, p.last_seen_at, p.expires_at,
                               p.created_at, p.updated_at
                        FROM chat_participant_facts_fts
                        JOIN chat_participant_facts p
                          ON p.fact_id = chat_participant_facts_fts.rowid
                        WHERE chat_participant_facts_fts.search_text MATCH ?
                          AND p.chat_id = ?
                          AND p.status = 'active'
                          AND (p.expires_at IS NULL OR p.expires_at >= ?)
                          {key_clause}
                        ORDER BY bm25(chat_participant_facts_fts), p.last_seen_at DESC
                        LIMIT ?
                        """,
                        [fts_query, chat_id, now_iso, *key_params, limit],
                    )
                    rows = await cursor.fetchall()
                    await cursor.close()
                return [_participant_fact_from_row(row) for row in rows]
            except aiosqlite.OperationalError:
                self._participant_facts_fts_available = False

        clauses = [
            "chat_id = ?",
            "status = 'active'",
            "(expires_at IS NULL OR expires_at >= ?)",
        ]
        params: list[object] = [chat_id, now_iso]
        if participant_keys:
            placeholders = ",".join("?" for _ in participant_keys)
            clauses.append(f"participant_key IN ({placeholders})")
            params.extend(participant_keys)
        if terms:
            term_clauses = []
            for term in terms:
                term_clauses.append(
                    "(lower(participant_name) LIKE ? OR lower(fact_type) LIKE ? "
                    "OR lower(fact_text) LIKE ?)"
                )
                pattern = f"%{term.lower()}%"
                params.extend([pattern, pattern, pattern])
            clauses.append(f"({' OR '.join(term_clauses)})")
        params.append(limit)
        async with aiosqlite.connect(self.database_path) as db:
            await self._prepare_connection(db)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""
                SELECT fact_id, chat_id, participant_key, participant_name,
                       fact_type, fact_text, source_message_ids, confidence,
                       status, first_seen_at, last_seen_at, expires_at,
                       created_at, updated_at
                FROM chat_participant_facts
                WHERE {' AND '.join(clauses)}
                ORDER BY last_seen_at DESC, fact_id DESC
                LIMIT ?
                """,
                params,
            )
            rows = await cursor.fetchall()
            await cursor.close()
        return [_participant_fact_from_row(row) for row in rows]

    async def mark_participant_facts_status(
        self,
        *,
        chat_id: int,
        participant_keys: list[str],
        status: str,
    ) -> int:
        if not participant_keys:
            return 0
        placeholders = ",".join("?" for _ in participant_keys)
        now_iso = datetime.now(timezone.utc).isoformat()
        async with self._write_lock:
            async with aiosqlite.connect(self.database_path) as db:
                await self._prepare_connection(db)
                cursor = await db.execute(
                    f"""
                    UPDATE chat_participant_facts
                    SET status = ?, updated_at = ?
                    WHERE chat_id = ? AND participant_key IN ({placeholders})
                      AND status = 'active'
                    """,
                    [status, now_iso, chat_id, *participant_keys],
                )
                count = cursor.rowcount
                await cursor.close()
                await db.commit()
        return int(count if count is not None and count >= 0 else 0)

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
    sender_id = row["sender_id"]
    return StoredMessage(
        message_id=int(row["message_id"]),
        chat_id=int(row["chat_id"]),
        chat_type=str(row["chat_type"]),
        sender_id=int(sender_id) if sender_id is not None else None,
        sender_name=str(row["sender_name"]),
        text=str(row["text"]) if text is None else text,
        created_at=str(row["created_at"]),
        reply_to_message_id=row["reply_to_message_id"],
        origin=str(row["origin"]) if "origin" in row.keys() else "legacy",
        kind=str(row["kind"]) if "kind" in row.keys() else "legacy_unclassified",
    )


async def _get_casino_spin(db: aiosqlite.Connection, spin_id: int) -> CasinoSpin | None:
    cursor = await db.execute("SELECT * FROM chat_casino_spins WHERE spin_id = ?", (spin_id,))
    row = await cursor.fetchone()
    await cursor.close()
    return _casino_spin_from_row(row) if row else None


def _casino_spin_from_row(row: aiosqlite.Row) -> CasinoSpin:
    return CasinoSpin(
        spin_id=int(row["spin_id"]),
        chat_id=int(row["chat_id"]),
        request_message_id=int(row["request_message_id"]),
        participant_key=str(row["participant_key"]),
        participant_name=str(row["participant_name"]),
        rules_version=str(row["rules_version"]),
        stake=int(row["stake"]),
        dice_message_id=int(row["dice_message_id"]) if row["dice_message_id"] is not None else None,
        dice_value=int(row["dice_value"]) if row["dice_value"] is not None else None,
        category=str(row["category"]) if row["category"] is not None else None,
        payout=int(row["payout"]) if row["payout"] is not None else None,
        status=str(row["status"]),
        terminal_balance=(int(row["terminal_balance"]) if row["terminal_balance"] is not None else None),
        terminal_reason=(str(row["terminal_reason"]) if row["terminal_reason"] is not None else None),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        completed_at=str(row["completed_at"]) if row["completed_at"] is not None else None,
        refunded_at=str(row["refunded_at"]) if row["refunded_at"] is not None else None,
    )


def _participant_key(sender_id: int | None, sender_name: str) -> str:
    if sender_id is not None:
        return f"id:{sender_id}"
    normalized = re.sub(r"[^\wа-яА-ЯёЁ]+", "_", sender_name.strip().lower()).strip("_")
    return f"name:{normalized or 'unknown'}"


def _normalized_participant_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _point_balance_from_row(row: aiosqlite.Row) -> PointBalance:
    return PointBalance(
        chat_id=int(row["chat_id"]),
        participant_key=str(row["participant_key"]),
        participant_name=str(row["participant_name"]),
        balance=int(row["balance"]),
        updated_at=str(row["updated_at"]),
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
        structured_json=str(row["structured_json"] or "{}"),
        level=str(row["level"] or "chunk"),
        created_at=str(row["created_at"]),
    )


def _participant_fact_from_row(row: aiosqlite.Row) -> ChatParticipantFact:
    return ChatParticipantFact(
        fact_id=int(row["fact_id"]),
        chat_id=int(row["chat_id"]),
        participant_key=str(row["participant_key"]),
        participant_name=str(row["participant_name"]),
        fact_type=str(row["fact_type"]),
        fact_text=str(row["fact_text"]),
        source_message_ids=str(row["source_message_ids"]),
        confidence=str(row["confidence"]),
        status=str(row["status"]),
        first_seen_at=str(row["first_seen_at"]),
        last_seen_at=str(row["last_seen_at"]),
        expires_at=row["expires_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _search_terms(text: str) -> list[str]:
    terms: list[str] = []
    for term in re.findall(r"[\wа-яА-ЯёЁ]{3,}", text.lower()):
        if term not in terms:
            terms.append(term)
    return terms[:12]


def _memory_block_search_text(
    *,
    summary: str,
    topics: str,
    keywords: str,
    structured_json: str,
    level: str,
) -> str:
    return " ".join(
        part.strip()
        for part in [summary, topics, keywords, structured_json, level]
        if part and part.strip()
    )


def _participant_fact_search_text(
    *,
    participant_name: str,
    fact_type: str,
    fact_text: str,
    confidence: str,
    status: str,
) -> str:
    return " ".join(
        part.strip()
        for part in [participant_name, fact_type, fact_text, confidence, status]
        if part and part.strip()
    )


def _merge_message_ids(existing_json: str, new_ids: list[int]) -> list[int]:
    try:
        existing = json.loads(existing_json)
    except json.JSONDecodeError:
        existing = []
    merged = {int(item) for item in existing if isinstance(item, int | str) and str(item).isdigit()}
    merged.update(new_ids)
    return sorted(merged)


def _best_confidence(first: str, second: str) -> str:
    ranks = {"low": 0, "medium": 1, "high": 2}
    first = first if first in ranks else "low"
    second = second if second in ranks else "low"
    return first if ranks[first] >= ranks[second] else second


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
