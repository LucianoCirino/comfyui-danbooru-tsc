"""LLM-callable search/lookup functions for the danbooru tag DBs.

Each function returns plain dicts/lists so the orchestrating agent can serialize
them straight into the OpenAI tool-message JSON.
"""

from __future__ import annotations

from typing import Any

try:
    from . import db as dblayer
    from .tagfmt import to_display_tag
except ImportError:  # script use
    import db as dblayer  # type: ignore
    from tagfmt import to_display_tag  # type: ignore


# ---------------------------------------------------------------------------
# Characters
# ---------------------------------------------------------------------------

def search_character(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Substring search by character name / copyright / trigger.

    Results are ordered by popularity (count) so the most recognizable matches
    come first, which is usually what the LLM wants when disambiguating a
    short user query.
    """
    if not query or not query.strip():
        return []
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    # Normalize the query to the same form the FTS index stores: underscore→
    # space for word-form input, untouched for emoticons. Lets callers pass
    # either `hatsune_miku` or `hatsune miku` and get the same result.
    fts_query = dblayer.fts_escape(to_display_tag(query))
    conn = dblayer.connect()
    try:
        rows = conn.execute(
            """
            SELECT c.character, c.copyright, c.trigger, c.core_tags,
                   c.count, c.popularity_rank
            FROM characters_fts f
            JOIN characters c ON c.id = f.rowid
            WHERE characters_fts MATCH ?
            ORDER BY c.count DESC
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "character": r["character"],
            "copyright": r["copyright"],
            "trigger": r["trigger"],
            "core_tags": r["core_tags"],
            "count": r["count"],
            "popularity_rank": r["popularity_rank"],
        }
        for r in rows
    ]


def lookup_character(character: str) -> dict[str, Any] | None:
    """Exact lookup by danbooru character key (e.g. 'hatsune_miku')."""
    key = (character or "").strip()
    if not key:
        return None
    conn = dblayer.connect()
    try:
        # Try exact key first; fall back to slug match on the human form.
        row = conn.execute(
            "SELECT character, copyright, trigger, core_tags, count, popularity_rank "
            "FROM characters WHERE character = ?",
            (key,),
        ).fetchone()
        if row is None:
            slug = key.replace(" ", "_").lower()
            row = conn.execute(
                "SELECT character, copyright, trigger, core_tags, count, popularity_rank "
                "FROM characters WHERE character = ?",
                (slug,),
            ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    return {
        "character": row["character"],
        "copyright": row["copyright"],
        "trigger": row["trigger"],
        "core_tags": row["core_tags"],
        "count": row["count"],
        "popularity_rank": row["popularity_rank"],
    }


# ---------------------------------------------------------------------------
# Artists
# ---------------------------------------------------------------------------

def search_artist(query: str, limit: int = 10) -> list[dict[str, Any]]:
    if not query or not query.strip():
        return []
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    # See search_character for the rationale on to_display_tag here.
    fts_query = dblayer.fts_escape(to_display_tag(query))
    conn = dblayer.connect()
    try:
        rows = conn.execute(
            """
            SELECT a.artist, a.trigger, a.count, a.popularity_rank
            FROM artists_fts f
            JOIN artists a ON a.id = f.rowid
            WHERE artists_fts MATCH ?
            ORDER BY a.count DESC
            LIMIT ?
            """,
            (fts_query, limit),
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "artist": r["artist"],
            "trigger": r["trigger"],
            "count": r["count"],
            "popularity_rank": r["popularity_rank"],
        }
        for r in rows
    ]


def lookup_artist(artist: str) -> dict[str, Any] | None:
    key = (artist or "").strip()
    if not key:
        return None
    conn = dblayer.connect()
    try:
        row = conn.execute(
            "SELECT artist, trigger, count, popularity_rank "
            "FROM artists WHERE artist = ?",
            (key,),
        ).fetchone()
        if row is None:
            slug = key.replace(" ", "_").lower()
            row = conn.execute(
                "SELECT artist, trigger, count, popularity_rank "
                "FROM artists WHERE artist = ?",
                (slug,),
            ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    return {
        "artist": row["artist"],
        "trigger": row["trigger"],
        "count": row["count"],
        "popularity_rank": row["popularity_rank"],
    }


# ---------------------------------------------------------------------------
# Tags (general vocabulary, with optional definition)
# ---------------------------------------------------------------------------

def search_tag(query: str, limit: int = 10, search_definition: bool = True) -> list[dict[str, Any]]:
    """Substring search over tag names + (optionally) definitions.

    Returns name-column matches first (ordered by post count), then — when
    ``search_definition`` is true — fills remaining slots with rows that
    matched only via the definition column. This two-pass ordering prevents
    popular tags whose *definition* incidentally contains the query (e.g.
    ``pants_tucked_in`` for query ``t_t``) from burying the true name match.
    """
    if not query or not query.strip():
        return []
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    # See search_character for the rationale on to_display_tag here.
    fts_query = dblayer.fts_escape(to_display_tag(query))
    name_query = f"name:{fts_query}"

    conn = dblayer.connect()
    try:
        name_rows = conn.execute(
            """
            SELECT t.id, t.tag, t.count, t.definition
            FROM tags_fts f
            JOIN tags t ON t.id = f.rowid
            WHERE tags_fts MATCH ?
            ORDER BY t.count DESC
            LIMIT ?
            """,
            (name_query, limit),
        ).fetchall()

        rows = list(name_rows)

        if search_definition and len(rows) < limit:
            extra = limit - len(rows)
            seen_ids = [r["id"] for r in rows]
            if seen_ids:
                placeholders = ",".join("?" * len(seen_ids))
                def_rows = conn.execute(
                    f"""
                    SELECT t.id, t.tag, t.count, t.definition
                    FROM tags_fts f
                    JOIN tags t ON t.id = f.rowid
                    WHERE tags_fts MATCH ?
                      AND t.id NOT IN ({placeholders})
                    ORDER BY t.count DESC
                    LIMIT ?
                    """,
                    (fts_query, *seen_ids, extra),
                ).fetchall()
            else:
                def_rows = conn.execute(
                    """
                    SELECT t.id, t.tag, t.count, t.definition
                    FROM tags_fts f
                    JOIN tags t ON t.id = f.rowid
                    WHERE tags_fts MATCH ?
                    ORDER BY t.count DESC
                    LIMIT ?
                    """,
                    (fts_query, extra),
                ).fetchall()
            rows.extend(def_rows)
    finally:
        conn.close()

    return [
        {
            "tag": r["tag"],
            "count": r["count"],
            # Truncate to keep the LLM context tight
            "definition": (r["definition"][:300] + "...") if len(r["definition"]) > 300 else r["definition"],
        }
        for r in rows
    ]


def lookup_tag(tag: str) -> dict[str, Any] | None:
    """Exact lookup by danbooru tag key (e.g. 'looking_at_viewer').

    Accepts either underscore_form or 'space form' (will try both).
    """
    if not tag or not (tag := tag.strip()):
        return None
    conn = dblayer.connect()
    try:
        row = conn.execute(
            "SELECT tag, count, definition FROM tags WHERE tag = ?",
            (tag,),
        ).fetchone()
        if row is None:
            slug = tag.replace(" ", "_").lower()
            row = conn.execute(
                "SELECT tag, count, definition FROM tags WHERE tag = ?",
                (slug,),
            ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"tag": row["tag"], "count": row["count"], "definition": row["definition"]}


# ---------------------------------------------------------------------------
# OpenAI-style tool specs (dispatched by the agent)
# ---------------------------------------------------------------------------

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "search_character",
            "description": (
                "Search the danbooru character database by name fragment, "
                "copyright/series, or trigger phrase. Returns up to `limit` "
                "matching characters ordered by post count (popularity). Use "
                "this when the user mentions a character by name or describes "
                "them by series and you need to find candidate matches."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text fragment, e.g. 'miku', 'reimu touhou', 'artoria fate'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of results to return (1-50). Default 10.",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_character",
            "description": (
                "Look up one character by exact danbooru key (e.g. "
                "'hatsune_miku'). Use this once you know the right key and "
                "want to confirm the trigger and core_tags."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "character": {
                        "type": "string",
                        "description": "Exact danbooru character tag, e.g. 'hatsune_miku'.",
                    },
                },
                "required": ["character"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_artist",
            "description": (
                "Search the danbooru artist database by artist name fragment. "
                "Returns up to `limit` matching artists ordered by post count. "
                "Use this when the user mentions an artist or art style by name."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-text fragment, e.g. 'ebifurya', 'wlop'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of results to return (1-50). Default 10.",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_artist",
            "description": (
                "Look up one artist by exact danbooru key (e.g. 'ebifurya'). "
                "Use this once you know the right key and want the trigger."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artist": {
                        "type": "string",
                        "description": "Exact danbooru artist tag, e.g. 'ebifurya'.",
                    },
                },
                "required": ["artist"],
            },
        },
    },
]


TAG_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "search_tag",
            "description": (
                "Search the danbooru general tag vocabulary by name or "
                "definition. Use this to find candidate tags for a concept "
                "the user described. Returns up to `limit` matching tags "
                "ordered by popularity (post count), with their definitions "
                "(truncated)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free text — e.g. 'sitting on bench', 'long sleeves', 'sukajan jacket'.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (1-50). Default 10.",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tag_definition",
            "description": (
                "Look up the definition of one specific danbooru tag. Use "
                "this when you have a candidate tag and want to verify what "
                "it actually means before keeping it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "Exact tag key, e.g. 'looking_at_viewer' or 'looking at viewer'.",
                    },
                },
                "required": ["tag"],
            },
        },
    },
]


# Dispatch map (name → callable). The agent calls dispatch(name, kwargs).
DISPATCH = {
    "search_character": search_character,
    "lookup_character": lookup_character,
    "search_artist": search_artist,
    "lookup_artist": lookup_artist,
    "search_tag": search_tag,
    "get_tag_definition": lookup_tag,
}


def dispatch(name: str, args: dict[str, Any]) -> Any:
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"Unknown tool: {name}"}
    try:
        return fn(**args) if args else fn()
    except TypeError as e:
        return {"error": f"Bad arguments for {name}: {e}"}
    except Exception as e:  # surfaced back to the model
        return {"error": f"{type(e).__name__}: {e}"}
