"""Convert the two danbooru CSVs into the unified SQLite database.

Run as a script:
    python build_db.py
    python build_db.py --character-csv "Z:\\path\\char.csv" --artist-csv "Z:\\path\\art.csv"

Or import build() and call programmatically — that's what the ComfyUI rebuild
node does.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    from ..core import db as dblayer  # imported as part of the package
    from ..core.tagfmt import to_display_tag
except ImportError:  # run directly: `python scripts/build_db.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core import db as dblayer  # type: ignore
    from core.tagfmt import to_display_tag  # type: ignore


def _row_iter(csv_path: Path):
    """Stream CSV rows, transparently raising on bad files."""
    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield row


_TAG_SPLIT_RE = re.compile(r"[,\n]")


def _split_tags(s: str) -> list[str]:
    if not s:
        return []
    return [t.strip().lower() for t in _TAG_SPLIT_RE.split(s) if t.strip()]


def build(character_csv: Path | None = None,
          artist_csv: Path | None = None,
          tags_csv: Path | None = None,
          progress=print) -> dict:
    """Rebuild danbooru.db from the three CSVs. Returns a stats dict.

    tags_csv is optional — if missing, the tags table is left empty (the agent
    + matcher will still work; only the tag refiner needs it).
    """
    char_path = Path(character_csv) if character_csv else dblayer.DEFAULT_CHARACTER_CSV
    art_path = Path(artist_csv) if artist_csv else dblayer.DEFAULT_ARTIST_CSV
    tags_path = Path(tags_csv) if tags_csv else dblayer.DEFAULT_TAGS_CSV

    if not char_path.is_file():
        raise FileNotFoundError(f"Character CSV not found: {char_path}")
    if not art_path.is_file():
        raise FileNotFoundError(f"Artist CSV not found: {art_path}")
    if not tags_path.is_file():
        progress(f"[danbooru-tsc] (warning) tags CSV not found: {tags_path} — tags table will be empty")
        tags_path = None

    start = time.time()
    progress(f"[danbooru-tsc] Building DB at {dblayer.DB_PATH}")

    # Wipe the file so we don't accumulate stale rows / FTS state across rebuilds.
    if dblayer.DB_PATH.exists():
        dblayer.DB_PATH.unlink()

    conn = dblayer.connect()
    try:
        dblayer.init_schema(conn)

        # Characters --------------------------------------------------------
        progress(f"[danbooru-tsc] Loading characters from {char_path.name}")
        char_rows = []
        char_tag_rows = []
        fts_char_rows = []
        for rank, row in enumerate(_row_iter(char_path)):
            character = (row.get("character") or "").strip()
            if not character:
                continue
            try:
                count = int(row.get("count") or 0)
            except ValueError:
                count = 0
            try:
                solo_count = int(row.get("solo_count") or 0)
            except ValueError:
                solo_count = 0
            trigger = (row.get("trigger") or to_display_tag(character)).strip()
            copyright_ = (row.get("copyright") or "").strip()
            core_tags = (row.get("core_tags") or "").strip()
            url = (row.get("url") or "").strip()
            char_rows.append((
                character, copyright_, trigger, core_tags,
                count, solo_count, url, rank,
            ))

        progress(f"[danbooru-tsc] Inserting {len(char_rows):,} characters")
        conn.executemany(
            "INSERT INTO characters(character, copyright, trigger, core_tags, "
            "count, solo_count, url, popularity_rank) VALUES (?,?,?,?,?,?,?,?)",
            char_rows,
        )
        conn.commit()

        # Build character_tags + FTS rows by re-querying so we have IDs.
        progress("[danbooru-tsc] Indexing character tags + FTS")
        cur = conn.execute(
            "SELECT id, character, copyright, trigger, core_tags FROM characters"
        )
        for cid, character, copyright_, trigger, core_tags in cur:
            tags = _split_tags(core_tags)
            for t in tags:
                char_tag_rows.append((cid, t))
            # FTS row: name uses spaces (better trigram tokenization),
            # plus copyright + trigger as searchable fields.
            fts_char_rows.append((
                cid,
                to_display_tag(character),
                to_display_tag(copyright_ or ""),
                trigger,
            ))

        conn.executemany(
            "INSERT INTO character_tags(character_id, tag) VALUES (?,?)",
            char_tag_rows,
        )
        conn.executemany(
            "INSERT INTO characters_fts(rowid, name, copyright, trigger) VALUES (?,?,?,?)",
            fts_char_rows,
        )
        conn.commit()

        # Artists -----------------------------------------------------------
        progress(f"[danbooru-tsc] Loading artists from {art_path.name}")
        art_rows = []
        for rank, row in enumerate(_row_iter(art_path)):
            artist = (row.get("artist") or "").strip()
            if not artist:
                continue
            try:
                count = int(row.get("count") or 0)
            except ValueError:
                count = 0
            trigger = (row.get("trigger") or to_display_tag(artist)).strip()
            url = (row.get("url") or "").strip()
            art_rows.append((artist, trigger, count, url, rank))

        progress(f"[danbooru-tsc] Inserting {len(art_rows):,} artists")
        conn.executemany(
            "INSERT INTO artists(artist, trigger, count, url, popularity_rank) "
            "VALUES (?,?,?,?,?)",
            art_rows,
        )
        conn.commit()

        progress("[danbooru-tsc] Indexing artist FTS")
        fts_art_rows = []
        cur = conn.execute("SELECT id, artist FROM artists")
        for aid, artist in cur:
            fts_art_rows.append((aid, to_display_tag(artist)))
        conn.executemany(
            "INSERT INTO artists_fts(rowid, name) VALUES (?,?)",
            fts_art_rows,
        )
        conn.commit()

        # Tags --------------------------------------------------------------
        if tags_path is not None:
            progress(f"[danbooru-tsc] Loading tags from {tags_path.name}")
            tag_rows = []
            tag_grouping_lookup = {}  # tag -> list of groups (for groupings table)
            with open(tags_path, "r", encoding="utf-8", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    tag = (row.get("tag") or "").strip()
                    if not tag:
                        continue
                    try:
                        cnt = int(row.get("count") or 0)
                    except ValueError:
                        cnt = 0
                    grouping = (row.get("grouping") or "").strip()
                    definition = (row.get("definition") or "").strip()
                    tag_rows.append((tag, cnt, grouping, definition))
                    if grouping:
                        tag_grouping_lookup[tag] = [g for g in grouping.split("|") if g]

            progress(f"[danbooru-tsc] Inserting {len(tag_rows):,} tags")
            conn.executemany(
                "INSERT OR IGNORE INTO tags(tag, count, grouping, definition) VALUES (?,?,?,?)",
                tag_rows,
            )
            conn.commit()

            progress(f"[danbooru-tsc] Indexing tag FTS + groupings")
            fts_tag_rows = []
            grouping_rows = []
            cur = conn.execute("SELECT id, tag, definition FROM tags")
            for tid, tag, definition in cur:
                fts_tag_rows.append((tid, to_display_tag(tag), definition))
                for g in tag_grouping_lookup.get(tag, []):
                    grouping_rows.append((tid, g))
            conn.executemany(
                "INSERT INTO tags_fts(rowid, name, definition) VALUES (?,?,?)",
                fts_tag_rows,
            )
            conn.executemany(
                "INSERT INTO tag_groupings(tag_id, grouping) VALUES (?,?)",
                grouping_rows,
            )
            conn.commit()
            progress(f"[danbooru-tsc] Inserted {len(grouping_rows):,} (tag, grouping) pairs")

        # Stamp build time + sources
        dblayer.set_meta(conn, "built_at", datetime.now().isoformat(timespec="seconds"))
        dblayer.set_meta(conn, "character_csv", str(char_path))
        dblayer.set_meta(conn, "artist_csv", str(art_path))
        if tags_path is not None:
            dblayer.set_meta(conn, "tags_csv", str(tags_path))
        conn.commit()

        conn.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()

    elapsed = time.time() - start
    progress(f"[danbooru-tsc] Build done in {elapsed:.1f}s")
    return dblayer.stats()


def _cli():
    p = argparse.ArgumentParser()
    p.add_argument("--character-csv", default=None)
    p.add_argument("--artist-csv", default=None)
    p.add_argument("--tags-csv", default=None)
    args = p.parse_args()
    s = build(args.character_csv, args.artist_csv, args.tags_csv)
    print("--- stats ---")
    for k, v in s.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    _cli()
