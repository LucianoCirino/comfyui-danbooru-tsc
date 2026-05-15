"""Scrape Danbooru wiki definitions for every tag in danbooru_tags.csv above a
post_count threshold, writing results to a new CSV.

Uses curl_cffi with Chrome impersonation to bypass the Cloudflare challenge.
Resumable: re-running picks up where it left off by reading the existing
output CSV and skipping rows already present.

Run as a script:
    python fetch_tag_definitions.py                      # full ~50k run
    python fetch_tag_definitions.py --limit 20           # quick smoke test
    python fetch_tag_definitions.py --threshold 1000     # smaller subset
    python fetch_tag_definitions.py --rate 3             # slower (3 req/s)
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import urllib.parse
from pathlib import Path

from curl_cffi import requests as cf_requests

try:
    from ..core import db as dblayer
except ImportError:  # run directly: `python scripts/fetch_tag_definitions.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core import db as dblayer  # type: ignore

DEFAULT_INPUT  = dblayer.DEFAULT_RAW_TAGS_CSV
DEFAULT_OUTPUT = dblayer.DEFAULT_TAGS_CSV
# Exclusion CSVs — any tag name appearing in these is skipped
DEFAULT_CHAR_CSV   = dblayer.DEFAULT_CHARACTER_CSV
DEFAULT_ARTIST_CSV = dblayer.DEFAULT_ARTIST_CSV

# ---------------------------------------------------------------------------
# DText -> plain text
# ---------------------------------------------------------------------------

# [[tag|alt text]] or [[tag|]] or [[tag]]
_LINK_WIKI = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]*))?\]\]")
# "label":[url]  or  "label":url
_LINK_EXT  = re.compile(r'"([^"]+?)":(?:\[([^\]]+)\]|(\S+?))(?=[\s.,;:!?)\]]|$)')
# [b]...[/b], [i], [u], [s], [tn]
_FORMAT    = re.compile(r"\[/?(?:b|i|u|s|tn|sub|sup|small|big|nodtext)\]", re.I)
# [color=red]...[/color], [quote], [/quote], [spoiler], [/spoiler], [expand=...], [/expand], [hr]
_BLOCK     = re.compile(r"\[/?(?:color|quote|spoiler|expand|code|hr|tn|center|table|tr|td|th|thead|tbody)(?:=[^\]]*)?\s*\]", re.I)
# h4. heading  /  h6#anchor. heading  -> drop the prefix only
_HEADING   = re.compile(r"^\s*h[1-6](?:#\S+)?\.\s*", re.M)
# Stand-alone example post lines:  * !post #1234
# or with caption:                  * !post #1234: caption  -> drop the line
_POST_LINE = re.compile(r"^\s*\*\s*!post\s*#\d+[^\n]*\n", re.M)
# Inline {{search_query}}  templates
_TEMPLATE  = re.compile(r"\{\{[^}]+\}\}")
# Multiple newlines -> max 2
_NEWLINES  = re.compile(r"\n{3,}")


_WS_RUN = re.compile(r"\s+")


def strip_dtext(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _LINK_WIKI.sub(lambda m: (m.group(2).strip() if m.group(2) else "") or m.group(1), s)
    s = _LINK_EXT.sub(lambda m: m.group(1), s)
    s = _FORMAT.sub("", s)
    s = _BLOCK.sub("", s)
    s = _HEADING.sub("", s)
    s = _POST_LINE.sub("", s)
    s = _TEMPLATE.sub("", s)
    # Collapse all whitespace (including newlines) to single spaces
    # so each CSV row is exactly one physical line.
    s = _WS_RUN.sub(" ", s)
    return s.strip()


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_one(session, tag: str, retries: int = 3) -> tuple[dict | None, str]:
    """Returns (data, status). status = 'ok' | '404' | 'http_xxx' | 'exc_xxx' | 'retries'."""
    encoded = urllib.parse.quote(tag, safe="")
    url = f"https://danbooru.donmai.us/wiki_pages/{encoded}.json"
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200:
                try:
                    return r.json(), "ok"
                except Exception:
                    return None, "bad_json"
            if r.status_code == 404:
                return None, "404"
            if r.status_code == 429:
                time.sleep(5 + 5 * attempt)
                continue
            if r.status_code in (500, 502, 503, 504):
                time.sleep(3 + 2 * attempt)
                continue
            return None, f"http_{r.status_code}"
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 + attempt)
                continue
            return None, f"exc_{type(e).__name__}"
    return None, "retries"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

FIELDS = ["tag", "count", "definition"]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input",     type=Path, default=DEFAULT_INPUT)
    p.add_argument("--output",    type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--exclude-character-csv", type=Path, default=DEFAULT_CHAR_CSV,
                   help="Skip tag names that appear in this CSV's 'character' column. Pass empty string to disable.")
    p.add_argument("--exclude-artist-csv", type=Path, default=DEFAULT_ARTIST_CSV,
                   help="Skip tag names that appear in this CSV's 'artist' column. Pass empty string to disable.")
    p.add_argument("--threshold", type=int, default=100,
                   help="Only fetch tags with post_count >= this (default 100).")
    p.add_argument("--rate",      type=float, default=5.0,
                   help="Max requests per second (default 5).")
    p.add_argument("--limit",     type=int, default=0,
                   help="Stop after this many fetches (0 = no limit). For testing.")
    p.add_argument("--profile",   default="chrome119",
                   help="curl_cffi impersonation profile (default chrome119).")
    p.add_argument("--flush-every", type=int, default=50,
                   help="Flush CSV to disk every N rows (default 50).")
    args = p.parse_args()

    if not args.input.is_file():
        print(f"ERROR: input not found: {args.input}", file=sys.stderr)
        return 1

    # Load exclusion sets (character + artist tag names)
    exclude: set[str] = set()
    for label, csv_path, key in (
        ("characters", args.exclude_character_csv, "character"),
        ("artists",    args.exclude_artist_csv,    "artist"),
    ):
        if csv_path and str(csv_path).strip() and Path(csv_path).is_file():
            n_before = len(exclude)
            with open(csv_path, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    name = (row.get(key) or "").strip()
                    if name:
                        exclude.add(name)
            print(f"  excluding {len(exclude) - n_before:,} {label} from {Path(csv_path).name}")

    # Read input, filter by threshold AND exclusion set
    print(f"Reading {args.input.name}")
    todo_all: list[tuple[str, int]] = []
    skipped_excluded = 0
    with open(args.input, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            try:
                pc = int(row.get("post_count") or 0)
            except ValueError:
                continue
            if pc < args.threshold:
                continue
            if name in exclude:
                skipped_excluded += 1
                continue
            todo_all.append((name, pc))
    print(f"  {len(todo_all):,} tags with post_count >= {args.threshold} (skipped {skipped_excluded:,} characters/artists)")

    # Resume: read existing output
    done: set[str] = set()
    if args.output.is_file():
        with open(args.output, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("tag"):
                    done.add(row["tag"])
        print(f"  {len(done):,} already in {args.output.name} (resume)")

    todo = [(n, pc) for n, pc in todo_all if n not in done]
    if args.limit > 0:
        todo = todo[: args.limit]
    print(f"  {len(todo):,} to fetch this run")
    if not todo:
        print("Nothing to do.")
        return 0

    # Open output in append mode
    new_file = not args.output.is_file()
    fout = open(args.output, "a", encoding="utf-8", newline="")
    writer = csv.DictWriter(fout, fieldnames=FIELDS)
    if new_file:
        writer.writeheader()
        fout.flush()

    session = cf_requests.Session(impersonate=args.profile)
    delay = 1.0 / max(args.rate, 0.1)

    start = time.time()
    counts = {"ok": 0, "404": 0, "err": 0}

    try:
        for i, (name, pc) in enumerate(todo, 1):
            t0 = time.time()
            data, status = fetch_one(session, name)

            if status == "ok":
                body = strip_dtext((data.get("body") or "") if data else "")
                writer.writerow({"tag": name, "count": pc, "definition": body})
                counts["ok"] += 1
            elif status == "404":
                writer.writerow({"tag": name, "count": pc, "definition": ""})
                counts["404"] += 1
            else:
                writer.writerow({"tag": name, "count": pc, "definition": ""})
                counts["err"] += 1

            if i % args.flush_every == 0 or i == len(todo):
                fout.flush()
                elapsed = time.time() - start
                rate = i / elapsed if elapsed > 0 else 0
                remaining = len(todo) - i
                eta_min = (remaining / rate / 60) if rate > 0 else 0
                print(
                    f"[{i:>6}/{len(todo)}] "
                    f"ok={counts['ok']:>5} 404={counts['404']:>5} err={counts['err']:>3}  "
                    f"rate={rate:.2f}/s  elapsed={elapsed/60:.1f}m  eta={eta_min:.1f}m"
                )

            # Polite rate limit
            spent = time.time() - t0
            if spent < delay:
                time.sleep(delay - spent)

    except KeyboardInterrupt:
        print("\nInterrupted — output flushed, safe to resume.")
    finally:
        fout.close()

    elapsed = time.time() - start
    print(f"\nDone in {elapsed/60:.1f}m. ok={counts['ok']} 404={counts['404']} err={counts['err']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
