"""
tracker.py — Database layer (aiosqlite).
Handles users, subscriptions, and the opportunities archive.
"""

import json
import aiosqlite
import asyncio
from dataclasses import dataclass
from typing import List, Optional
from datetime import datetime

DB_PATH = "lumo.db"

# ─── Schema ───────────────────────────────────────────────────────────────────

CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id                  INTEGER PRIMARY KEY,   -- Telegram user_id
    username            TEXT,
    subscription_status TEXT NOT NULL DEFAULT 'free',  -- 'free' | 'pro'
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS channels (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    tg_id    INTEGER NOT NULL UNIQUE  -- Telegram channel peer_id
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    goal       TEXT NOT NULL DEFAULT 'monitor',   -- monitor | digest | alerts | summary
    keywords   TEXT NOT NULL DEFAULT '[]',        -- JSON array of strings
    cadence    TEXT NOT NULL DEFAULT 'immediate', -- immediate | daily | weekly
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, channel_id)
);

CREATE TABLE IF NOT EXISTS opportunities_archive (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_tg_id  INTEGER NOT NULL,
    raw_text       TEXT NOT NULL,
    structured_json TEXT,                         -- JSON: title/deadline/link/summary
    timestamp      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_subs_channel   ON subscriptions(channel_id);
CREATE INDEX IF NOT EXISTS idx_subs_user      ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_archive_ts     ON opportunities_archive(timestamp);
CREATE INDEX IF NOT EXISTS idx_archive_ch     ON opportunities_archive(channel_tg_id);
"""

# ─── Dataclasses ─────────────────────────────────────────────────────────────

@dataclass
class Subscription:
    user_id: int
    channel_tg_id: int
    channel_username: str
    goal: str
    keywords: List[str]
    cadence: str
    active: bool = True
    audience_criteria: str = ""

@dataclass
class Opportunity:
    id: int
    channel_tg_id: int
    raw_text: str
    structured: Optional[dict]
    timestamp: str

# ─── Init ─────────────────────────────────────────────────────────────────────

async def _migrate_schema(db: aiosqlite.Connection) -> None:
    async with db.execute("PRAGMA table_info(users)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if "audience_criteria" not in cols:
        await db.execute(
            "ALTER TABLE users ADD COLUMN audience_criteria TEXT NOT NULL DEFAULT ''"
        )


async def init_db() -> None:
    """Create all tables on startup. Safe to call multiple times."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_TABLES)
        await _migrate_schema(db)
        await db.commit()
    print("[DB] Database initialised.")

# ─── Users ────────────────────────────────────────────────────────────────────

async def upsert_user(user_id: int, username: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users(id, username) VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET username = excluded.username
            """,
            (user_id, username),
        )
        await db.commit()

async def get_user(user_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_user_criteria(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT audience_criteria FROM users WHERE id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return (row[0] or "").strip() if row else ""


async def set_user_criteria(user_id: int, criteria: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users(id, username, audience_criteria) VALUES (?, '', ?)
            ON CONFLICT(id) DO UPDATE SET audience_criteria = excluded.audience_criteria
            """,
            (user_id, criteria.strip()),
        )
        await db.commit()

# ─── Channels ─────────────────────────────────────────────────────────────────

async def _get_or_create_channel(db: aiosqlite.Connection, username: str, tg_id: int) -> int:
    """Returns internal channel.id (auto-increment PK)."""
    await db.execute(
        """
        INSERT INTO channels(username, tg_id) VALUES (?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username
        """,
        (username, tg_id),
    )
    async with db.execute("SELECT id FROM channels WHERE tg_id = ?", (tg_id,)) as cur:
        row = await cur.fetchone()
        return row[0]

# ─── Subscriptions ────────────────────────────────────────────────────────────

async def add_subscription(
    user_id: int,
    username: str,   # channel @handle
    tg_id: int,      # Telegram peer_id
    goal: str,
    keywords: List[str],
    cadence: str,
) -> bool:
    """Returns True if inserted, False if already exists."""
    async with aiosqlite.connect(DB_PATH) as db:
        channel_pk = await _get_or_create_channel(db, username, tg_id)
        try:
            await db.execute(
                """
                INSERT INTO subscriptions(user_id, channel_id, goal, keywords, cadence)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, channel_pk, goal, json.dumps(keywords, ensure_ascii=False), cadence),
            )
            await db.commit()
            print(f"[DB] Subscription added: user={user_id} -> @{username}")
            return True
        except aiosqlite.IntegrityError:
            print(f"[DB] Duplicate subscription: user={user_id} -> @{username}")
            return False

async def remove_subscription(user_id: int, channel_username: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT id FROM channels WHERE username = ?", (channel_username,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        channel_pk = row[0]
        result = await db.execute(
            "DELETE FROM subscriptions WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_pk),
        )
        await db.commit()
        return result.rowcount > 0

async def list_subscriptions(user_id: int) -> List[Subscription]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT s.user_id, s.goal, s.keywords, s.cadence, s.active,
                   c.username, c.tg_id,
                   COALESCE(u.audience_criteria, '') AS audience_criteria
            FROM subscriptions s
            JOIN channels c ON c.id = s.channel_id
            LEFT JOIN users u ON u.id = s.user_id
            WHERE s.user_id = ? AND s.active = 1
            """,
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [
        Subscription(
            user_id=r["user_id"],
            channel_tg_id=r["tg_id"],
            channel_username=r["username"],
            goal=r["goal"],
            keywords=json.loads(r["keywords"]),
            cadence=r["cadence"],
            active=bool(r["active"]),
            audience_criteria=r["audience_criteria"] or "",
        )
        for r in rows
    ]

async def get_subscribers_for_channel(channel_tg_id: int) -> List[Subscription]:
    """
    All active subscriptions for a given Telegram channel peer_id.
    This is the hot path called for every incoming post.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT s.user_id, s.goal, s.keywords, s.cadence,
                   c.username, c.tg_id,
                   COALESCE(u.audience_criteria, '') AS audience_criteria
            FROM subscriptions s
            JOIN channels c ON c.id = s.channel_id
            LEFT JOIN users u ON u.id = s.user_id
            WHERE c.tg_id = ? AND s.active = 1
            """,
            (channel_tg_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [
        Subscription(
            user_id=r["user_id"],
            channel_tg_id=r["tg_id"],
            channel_username=r["username"],
            goal=r["goal"],
            keywords=json.loads(r["keywords"]),
            cadence=r["cadence"],
            audience_criteria=r["audience_criteria"] or "",
        )
        for r in rows
    ]

async def get_all_tracked_tg_ids() -> List[int]:
    """Returns unique Telegram channel IDs that have at least one active subscriber."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """
            SELECT DISTINCT c.tg_id FROM channels c
            JOIN subscriptions s ON s.channel_id = c.id
            WHERE s.active = 1
            """
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]

# ─── Opportunities Archive ─────────────────────────────────────────────────────

async def archive_opportunity(
    channel_tg_id: int, raw_text: str, structured: Optional[dict]
) -> int:
    """Saves a processed opportunity. Returns its new id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            """
            INSERT INTO opportunities_archive(channel_tg_id, raw_text, structured_json)
            VALUES (?, ?, ?)
            """,
            (
                channel_tg_id,
                raw_text,
                json.dumps(structured, ensure_ascii=False) if structured else None,
            ),
        )
        await db.commit()
        return cur.lastrowid

async def search_archive(query: str, limit: int = 10) -> List[Opportunity]:
    """
    Full-text keyword search across raw_text and structured_json.
    For production, consider FTS5 virtual table for better performance.
    """
    pattern = f"%{query}%"
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """
            SELECT id, channel_tg_id, raw_text, structured_json, timestamp
            FROM opportunities_archive
            WHERE raw_text LIKE ? OR structured_json LIKE ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (pattern, pattern, limit),
        ) as cur:
            rows = await cur.fetchall()
    return [
        Opportunity(
            id=r["id"],
            channel_tg_id=r["channel_tg_id"],
            raw_text=r["raw_text"],
            structured=json.loads(r["structured_json"]) if r["structured_json"] else None,
            timestamp=r["timestamp"],
        )
        for r in rows
    ]