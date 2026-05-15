"""Shared SQLite layer for the danbooru-tsc node pack.

One database file (danbooru.db) holds three tables — characters, character_tags
(for tag-based matching), and artists — plus FTS5 trigram virtual tables that
back the LLM-callable search functions.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

# db.py now lives at <pack_root>/core/db.py, but csv/, prompts/, and
# danbooru.db all sit at <pack_root>/, so step up one level.
PACK_DIR = Path(__file__).resolve().parent.parent
DB_PATH = PACK_DIR / "danbooru.db"

DEFAULT_CSV_DIR = PACK_DIR / "csv"
DEFAULT_CHARACTER_CSV = DEFAULT_CSV_DIR / "danbooru_character.csv"
DEFAULT_ARTIST_CSV = DEFAULT_CSV_DIR / "danbooru_artist.csv"
DEFAULT_TAGS_CSV = DEFAULT_CSV_DIR / "danbooru_tags_with_definitions.csv"
DEFAULT_RAW_TAGS_CSV = DEFAULT_CSV_DIR / "danbooru_tags.csv"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS characters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    character TEXT UNIQUE NOT NULL,
    copyright TEXT,
    trigger TEXT NOT NULL,
    core_tags TEXT,
    count INTEGER,
    solo_count INTEGER,
    url TEXT,
    popularity_rank INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chars_pop ON characters(popularity_rank);
CREATE INDEX IF NOT EXISTS idx_chars_copyright ON characters(copyright);

CREATE TABLE IF NOT EXISTS character_tags (
    character_id INTEGER NOT NULL,
    tag TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ct_tag ON character_tags(tag);
CREATE INDEX IF NOT EXISTS idx_ct_char ON character_tags(character_id);

CREATE TABLE IF NOT EXISTS artists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT UNIQUE NOT NULL,
    trigger TEXT NOT NULL,
    count INTEGER,
    url TEXT,
    popularity_rank INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artists_pop ON artists(popularity_rank);

CREATE VIRTUAL TABLE IF NOT EXISTS characters_fts USING fts5(
    name, copyright, trigger,
    content='', tokenize='trigram'
);

CREATE VIRTUAL TABLE IF NOT EXISTS artists_fts USING fts5(
    name,
    content='', tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT UNIQUE NOT NULL,
    count INTEGER NOT NULL,
    grouping TEXT NOT NULL DEFAULT '',
    definition TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tags_count ON tags(count DESC);

CREATE TABLE IF NOT EXISTS tag_groupings (
    tag_id INTEGER NOT NULL,
    grouping TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tg_grouping ON tag_groupings(grouping);
CREATE INDEX IF NOT EXISTS idx_tg_tag ON tag_groupings(tag_id);

CREATE VIRTUAL TABLE IF NOT EXISTS tags_fts USING fts5(
    name, definition,
    content='', tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

_lock = threading.Lock()


def db_exists() -> bool:
    return DB_PATH.exists() and DB_PATH.stat().st_size > 0


def connect() -> sqlite3.Connection:
    """Open a connection. Caller is responsible for closing."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def stats() -> dict:
    """Quick row counts for UI feedback."""
    if not db_exists():
        return {"exists": False}
    conn = connect()
    try:
        c = conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
        a = conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0]
        ct = conn.execute("SELECT COUNT(*) FROM character_tags").fetchone()[0]
        try:
            t = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
            t_with_def = conn.execute("SELECT COUNT(*) FROM tags WHERE definition != ''").fetchone()[0]
            t_with_group = conn.execute("SELECT COUNT(*) FROM tags WHERE grouping != ''").fetchone()[0]
            n_groupings = conn.execute("SELECT COUNT(*) FROM tag_groupings").fetchone()[0]
            n_unique_groups = conn.execute("SELECT COUNT(DISTINCT grouping) FROM tag_groupings").fetchone()[0]
        except Exception:
            t = t_with_def = t_with_group = n_groupings = n_unique_groups = 0
        built = get_meta(conn, "built_at") or "(unknown)"
        return {
            "exists": True,
            "characters": c,
            "artists": a,
            "character_tags": ct,
            "tags": t,
            "tags_with_definition": t_with_def,
            "tags_with_grouping": t_with_group,
            "tag_grouping_pairs": n_groupings,
            "unique_groupings": n_unique_groups,
            "built_at": built,
            "path": str(DB_PATH),
        }
    finally:
        conn.close()


def fts_escape(query: str) -> str:
    """Escape a user query for safe FTS5 MATCH usage.

    With the trigram tokenizer, the simplest reliable approach is to wrap each
    whitespace-delimited token in double quotes (so parentheses, hyphens, etc.
    are treated as literal characters, not FTS operators).
    """
    tokens = [t for t in query.replace('"', " ").split() if t.strip()]
    if not tokens:
        return '""'
    return " ".join(f'"{t}"' for t in tokens)
