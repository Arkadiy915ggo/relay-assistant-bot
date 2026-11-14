"""SQLite candidate retrieval across all saved history, without model work."""

from __future__ import annotations

import re
import time
from datetime import datetime

import aiosqlite


STOP_WORDS = frozenset("найди найти поищи отыщи разыщи ищи выполни сделай проведи запусти поиск сообщение сообщения сообщением где я мои мой мне мы ты он она это что как когда про для по на в с и или было был была говорил писал рассказывал давал пожалуйста".split())


def terms(text: str) -> list[str]:
    words = re.findall(r"[^\W_]+", text.casefold().replace("ё", "е"))
    return list(dict.fromkeys(w for w in words if w not in STOP_WORDS and len(w) >= 2))[:12]


def prefix(word: str) -> str:
    # Broad candidate recall only; the model must still judge relevance.
    return word[:-2] if len(word) >= 6 and re.fullmatch(r"[а-я]+", word) else word


async def init_search_index(db: aiosqlite.Connection) -> bool:
    cursor = await db.execute("SELECT 1 FROM sqlite_master WHERE name='messages_search_fts'")
    exists = await cursor.fetchone()
    await cursor.close()
    try:
        await db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS messages_search_fts USING fts5(text, tokenize='unicode61')")
    except aiosqlite.OperationalError as exc:
        if 'no such module' not in str(exc).lower():
            raise
        return False
    for name, event, body in (
        ('insert', 'AFTER INSERT', "INSERT INTO messages_search_fts(rowid,text) VALUES(new.rowid,replace(replace(new.text,'ё','е'),'Ё','Е'));"),
        ('delete', 'AFTER DELETE', "DELETE FROM messages_search_fts WHERE rowid=old.rowid;"),
        ('update', 'AFTER UPDATE', "DELETE FROM messages_search_fts WHERE rowid=old.rowid; INSERT INTO messages_search_fts(rowid,text) VALUES(new.rowid,replace(replace(new.text,'ё','е'),'Ё','Е'));"),
    ):
        await db.execute(f"CREATE TRIGGER IF NOT EXISTS messages_search_{name} {event} ON messages BEGIN {body} END")
    if not exists:
        await db.execute("INSERT INTO messages_search_fts(rowid,text) SELECT rowid,replace(replace(text,'ё','е'),'Ё','Е') FROM messages")
    return True


async def retrieve_candidates(
    db: aiosqlite.Connection, *, fts: bool, chat_id: int, before_id: int,
    queries: list[str], sender_id: int | None, since: datetime | None, limit: int = 30,
) -> list[dict]:
    if not 1 <= limit <= 30 or not 1 <= len(queries) <= 7:
        raise ValueError('invalid candidate search bounds')
    groups = list(dict.fromkeys(tuple(prefix(w) for w in terms(q)) for q in queries))
    groups = [g for g in groups if g]
    if not groups:
        return []
    deadline = time.monotonic() + 10
    await db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
    scope = """m.chat_id=? AND m.message_id<? AND (? IS NULL OR m.sender_id=?)
        AND (? IS NULL OR m.created_at>=?)
        AND (m.origin='legacy' OR (m.origin='incoming' AND m.kind IN ('text','caption','voice_transcript')))"""
    params = (chat_id, before_id, sender_id, sender_id, since.isoformat() if since else None, since.isoformat() if since else None)
    scores: dict[int, float] = {}
    sources: dict[int, dict] = {}
    for group in groups:
        def score(text: str) -> int:
            words = re.findall(r'[^\W_]+', text.casefold().replace('ё', 'е'))
            return sum(any(w.startswith(term) for w in words) for term in group)

        await db.create_function('search_candidate_score', 1, score)
        if fts:
            match = ' OR '.join(f'"{word}"*' for word in group)
            cursor = await db.execute(
                f"""SELECT m.* FROM messages_search_fts JOIN messages m ON m.rowid=messages_search_fts.rowid
                    WHERE messages_search_fts MATCH ? AND {scope}
                    ORDER BY search_candidate_score(m.text) DESC, bm25(messages_search_fts), m.message_id DESC LIMIT ?""",
                (match, *params, limit),
            )
        else:
            cursor = await db.execute(
                f"SELECT m.* FROM messages m WHERE {scope} AND search_candidate_score(m.text)>0 ORDER BY search_candidate_score(m.text) DESC,m.message_id DESC LIMIT ?",
                (*params, limit),
            )
        rows = await cursor.fetchall()
        await cursor.close()
        for rank, row in enumerate(rows):
            mid = row['message_id']
            sources[mid] = dict(row)
            scores[mid] = scores.get(mid, 0) + 1 / (30 + rank)
    return [sources[mid] for mid in sorted(scores, key=lambda mid: (-scores[mid], -mid))[:limit]]
