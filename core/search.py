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


# Hard ceiling on how many rows the substring search tools (character /
# artist / tag) will return. The DanbooruAgent's `search_limit` widget can
# drive the requested limit all the way up to this value; the ceiling exists
# only as a guardrail against pathological inputs, not as a product limit.
# Practical FTS matches return far fewer rows than this in any case.
MAX_SEARCH_LIMIT = 10000


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
    if limit > MAX_SEARCH_LIMIT:
        limit = MAX_SEARCH_LIMIT

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
    if limit > MAX_SEARCH_LIMIT:
        limit = MAX_SEARCH_LIMIT

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
    if limit > MAX_SEARCH_LIMIT:
        limit = MAX_SEARCH_LIMIT

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
            # Emit in display form (underscores → spaces, lowercased) so the
            # LLM that copies our result into its final prompt naturally uses
            # the form Anima expects.
            "tag": to_display_tag(r["tag"]),
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
    # Display form so the LLM uses spaces (not underscores) when copying.
    return {"tag": to_display_tag(row["tag"]), "count": row["count"], "definition": row["definition"]}


def list_tag_groupings(filter: str = "", limit: int = 30) -> list[dict[str, Any]]:
    """List available danbooru tag groupings (categories), optionally filtered.

    The DB has ~950 distinct groupings, often hierarchical with ``:`` separators
    (e.g. ``image_composition:framing_the_body``, ``posture:standing``). Returns
    groupings ordered by number of tags they contain.
    """
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    conn = dblayer.connect()
    try:
        if filter and filter.strip():
            rows = conn.execute(
                """
                SELECT grouping, COUNT(*) AS n
                FROM tag_groupings
                WHERE grouping LIKE ?
                GROUP BY grouping
                ORDER BY n DESC
                LIMIT ?
                """,
                (f"%{filter.strip()}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT grouping, COUNT(*) AS n
                FROM tag_groupings
                GROUP BY grouping
                ORDER BY n DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [{"grouping": r["grouping"], "tag_count": r[1]} for r in rows]


def list_tags_in_grouping(grouping: str, limit: int = 20) -> list[dict[str, Any]]:
    """List tags within a specific grouping, ordered by popularity (post count).

    Use after ``list_tag_groupings`` finds the right category. Tag names are
    emitted in display form (spaces, not underscores) so the LLM can copy them
    straight into a prompt.
    """
    if not grouping or not grouping.strip():
        return []
    if limit < 1:
        limit = 1
    if limit > 100:
        limit = 100
    conn = dblayer.connect()
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT t.tag, t.count
            FROM tag_groupings tg
            JOIN tags t ON t.id = tg.tag_id
            WHERE tg.grouping = ?
            ORDER BY t.count DESC
            LIMIT ?
            """,
            (grouping.strip(), limit),
        ).fetchall()
    finally:
        conn.close()
    return [{"tag": to_display_tag(r["tag"]), "count": r["count"]} for r in rows]


def lookup_tags(tags: list[str], include_grouping: bool = False) -> list[dict[str, Any]]:
    """Bulk exact-lookup for a list of tag keys. Returns one entry per input
    in input order. Missing tags get ``{"tag": <input>, "found": False}`` so
    the LLM can tell which DanBot suggestions don't exist in the DB.

    One SQL round-trip regardless of input size, with both display-form and
    underscore-slug attempted for each input.

    ``include_grouping`` adds a ``grouping`` key (the raw pipe-separated
    ``tags.grouping`` string) to each found entry. Off by default so the
    agent's ``get_tag_definitions`` tool output stays unchanged; the
    DanbooruTagAnnotator opts in when its "include_group" toggle is on.
    """
    if not tags:
        return []
    cleaned = [(t or "").strip() for t in tags]
    # Build the set of candidate spellings we'll query in one shot.
    candidates: set[str] = set()
    for c in cleaned:
        if not c:
            continue
        lo = c.lower()
        candidates.add(lo)
        candidates.add(lo.replace(" ", "_"))
    if not candidates:
        return [{"tag": orig, "found": False} for orig in cleaned]
    cols = "tag, count, definition" + (", grouping" if include_grouping else "")
    conn = dblayer.connect()
    try:
        placeholders = ",".join("?" * len(candidates))
        rows = conn.execute(
            f"SELECT {cols} FROM tags WHERE tag IN ({placeholders})",
            list(candidates),
        ).fetchall()
    finally:
        conn.close()
    found: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = {"tag": r["tag"], "count": r["count"], "definition": r["definition"]}
        if include_grouping:
            entry["grouping"] = r["grouping"]
        found[r["tag"]] = entry
    out: list[dict[str, Any]] = []
    for orig in cleaned:
        if not orig:
            out.append({"tag": orig, "found": False})
            continue
        lo = orig.lower()
        hit = found.get(lo) or found.get(lo.replace(" ", "_"))
        if hit:
            # Truncate long definitions to keep context tight (matches search_tag).
            d = hit["definition"]
            entry = {
                # Display form (spaces, not underscores) so the LLM copies
                # the form Anima expects.
                "tag": to_display_tag(hit["tag"]),
                "count": hit["count"],
                "definition": (d[:300] + "...") if len(d) > 300 else d,
            }
            if include_grouping:
                entry["grouping"] = hit.get("grouping", "")
            out.append(entry)
        else:
            out.append({"tag": orig, "found": False})
    return out


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
                "SUBSTRING search over danbooru tag names + definitions. "
                "Keep queries SHORT — one concept per call, 1-2 words max. "
                "Multi-concept queries like 'walking outdoors park' look for "
                "a single tag containing ALL those words, which almost never "
                "exists; they will return empty. For several unrelated "
                "concepts, issue PARALLEL `search_tag` tool_calls in ONE "
                "response (do not spread them across iterations). "
                "Examples of good queries: 'walking', 'outdoors', 'sunny', "
                "'bokeh', 'shy', 'soft smile'. Returns up to `limit` "
                "matching tags ordered by popularity (post count)."
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
    {
        "type": "function",
        "function": {
            "name": "list_tag_groupings",
            "description": (
                "List danbooru tag groupings (categories). Use this BEFORE "
                "search_tag when looking for a categorical concept where you "
                "don't know the canonical tag name. Common groupings: "
                "image_composition, image_composition:framing_the_body, "
                "posture, eyes_tags, face_tags, hair_styles, attire, "
                "headwear, accessories, locations, body_parts, "
                "verbs_and_gerunds. Optional `filter` is a substring match "
                "on grouping names. Returns groupings ordered by number of "
                "tags they contain."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Substring to filter grouping names (e.g. 'composition', 'eyes', 'posture'). Empty = list top groupings.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1-100). Default 30.",
                        "default": 30,
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tags_in_grouping",
            "description": (
                "List tags within a specific grouping, ordered by popularity. "
                "Use AFTER list_tag_groupings finds the right category. "
                "Example: list_tags_in_grouping('image_composition:framing_the_body') "
                "returns the actual valid framing tags ('full body', "
                "'upper body', 'cowboy shot', 'portrait', ...) rather than "
                "you guessing free-text names like 'medium shot' that aren't "
                "real tags."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "grouping": {
                        "type": "string",
                        "description": "Exact grouping name returned by list_tag_groupings.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1-100). Default 20.",
                        "default": 20,
                    },
                },
                "required": ["grouping"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_tag_definitions",
            "description": (
                "Bulk version of get_tag_definition. Pass a list of tag names "
                "and get back their definitions in one call. Returns one entry "
                "per input tag, in input order. Missing tags get "
                "{tag: <input>, found: false} so you can tell which DanBot "
                "candidates don't exist in the DB. Use this to verify all "
                "DanBot tags up-front against the description before deciding "
                "what to keep — more efficient than calling get_tag_definition "
                "one at a time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of tag names to look up.",
                    },
                },
                "required": ["tags"],
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
    "get_tag_definitions": lookup_tags,
    "list_tag_groupings": list_tag_groupings,
    "list_tags_in_grouping": list_tags_in_grouping,
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
