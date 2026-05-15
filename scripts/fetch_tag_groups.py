"""Scrape Danbooru tag_group:* wiki pages and add a 'grouping' column to
danbooru_tags_with_definitions.csv.

For each tag_group page, parse member tags from [[wiki_link]] references,
cross-validate against the tags table, and build a {tag: {groups}} mapping.

Output CSV schema: tag, count, grouping, definition
where `grouping` is pipe-separated (e.g. 'hair_styles|face_tags').

Run:
    python fetch_tag_groups.py
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import urllib.parse
from collections import defaultdict
from pathlib import Path

from curl_cffi import requests as cf_requests

try:
    from ..core import db as dblayer
except ImportError:  # run directly: `python scripts/fetch_tag_groups.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from core import db as dblayer  # type: ignore

DEFAULT_TAGS_CSV = dblayer.DEFAULT_TAGS_CSV

# Match [[anything]] or [[anything|alt text]]; we only want group(1)
_LINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")
# Trim trailing #anchor (e.g. [[hair_styles#dtext-tied]])
_ANCHOR_RE = re.compile(r"#[^#]*$")
# dtext section header: "h4. Title", "h4#anchor. Title", "h5#dtext-foo. Bar"
_HEADER_RE = re.compile(r"^h([1-6])(?:#[\w-]+)?\.\s+(.+?)\s*$", re.MULTILINE)
# Strip dtext inline markup ([b]...[/b], [i]...[/i], "Label":/path) from header titles
_DTEXT_INLINE_RE = re.compile(r"\[/?[a-z]+\]|\"[^\"]*\":\S+", re.IGNORECASE)


def _slugify_section(title: str) -> str:
    """Normalize a section header title into a stable slug.

    "View Angle" -> "view_angle"
    "Perspective/Depth" -> "perspective_depth"
    "Background/Color" -> "background_color"
    """
    # Drop inline dtext markup before slugifying
    cleaned = _DTEXT_INLINE_RE.sub("", title).strip().lower()
    # Whitespace and slashes both become single underscores
    cleaned = re.sub(r"[\s/]+", "_", cleaned)
    # Drop anything that isn't word char, hyphen, or underscore
    cleaned = re.sub(r"[^\w\-]+", "", cleaned)
    # Collapse runs of underscores and trim ends
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned


def list_tag_groups(session) -> list[str]:
    """Page through wiki_pages.json and collect every tag_group:* title."""
    titles = []
    page = 1
    while True:
        r = session.get(
            "https://danbooru.donmai.us/wiki_pages.json",
            params={
                "search[title_normalize]": "tag_group:*",
                "limit": 200,
                "page": page,
                "only": "title",
            },
            timeout=20,
        )
        if r.status_code != 200:
            raise RuntimeError(f"Listing failed at page {page}: HTTP {r.status_code}")
        data = r.json()
        if not data:
            break
        titles.extend(p["title"] for p in data if p.get("title", "").startswith("tag_group:"))
        if len(data) < 200:
            break
        page += 1
    return sorted(set(titles))


def fetch_group_body(session, title: str) -> str | None:
    encoded = urllib.parse.quote(title, safe="")
    r = session.get(f"https://danbooru.donmai.us/wiki_pages/{encoded}.json", timeout=20)
    if r.status_code != 200:
        return None
    return r.json().get("body", "")


def parse_member_tags(body: str) -> dict[str, set[str]]:
    """Pull every [[X]] from the body and bucket each by the most recent dtext
    section header above it.

    Returns: {tag_slug: {section_slug or "" for top-of-body}}
    An empty-string section means the tag appeared before any header — its
    only attribution is the parent tag_group itself.
    """
    if not body:
        return {}

    # Collect section header line spans and their slugs. We track the full
    # (start, end) span of the header line so we can skip [[links]] that
    # appear inside a header (e.g. "h5. [[Traditional media]]") — those are
    # part of the section's title, not its body.
    header_spans: list[tuple[int, int, str]] = []  # (line_start, line_end, slug)
    for hm in _HEADER_RE.finditer(body):
        slug = _slugify_section(hm.group(2))
        if slug:
            header_spans.append((hm.start(), hm.end(), slug))

    def section_for(pos: int) -> str:
        # Most recent header whose line ends at or before this position.
        current = ""
        for _, h_end, h_slug in header_spans:
            if h_end <= pos:
                current = h_slug
            else:
                break
        return current

    def inside_header(pos: int) -> bool:
        for h_start, h_end, _ in header_spans:
            if h_start <= pos < h_end:
                return True
            if h_start > pos:
                break
        return False

    out: dict[str, set[str]] = defaultdict(set)
    for m in _LINK_RE.finditer(body):
        if inside_header(m.start()):
            continue
        ref = m.group(1).strip()
        ref = _ANCHOR_RE.sub("", ref)
        if not ref:
            continue
        # Skip self-references and meta. Reference text may use either spaces
        # or underscores between "tag" and "group", so normalize before testing.
        low_compact = ref.lower().replace(" ", "_")
        if low_compact.startswith("tag_group") or low_compact == "list":
            continue
        slug = ref.replace(" ", "_").lower()
        out[slug].add(section_for(m.start()))
    return dict(out)


def load_known_tags(csv_path: Path) -> set[str]:
    """Set of tag keys that exist in the tags CSV (so we filter out non-existent member references)."""
    known = set()
    with open(csv_path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            t = (row.get("tag") or "").strip()
            if t:
                known.add(t)
    return known


def build_tag_to_groups(session, group_titles: list[str], known_tags: set[str],
                       rate_per_sec: float = 5.0, progress=print) -> dict[str, set[str]]:
    delay = 1.0 / max(rate_per_sec, 0.1)
    tag_to_groups: dict[str, set[str]] = defaultdict(set)
    start = time.time()
    skipped_unknown_total = 0

    for i, title in enumerate(group_titles, 1):
        t0 = time.time()
        body = fetch_group_body(session, title)
        if body is None:
            progress(f"  [{i}/{len(group_titles)}] {title} -> FAILED")
        else:
            members = parse_member_tags(body)
            kept = {t: secs for t, secs in members.items() if t in known_tags}
            skipped_unknown_total += len(members) - len(kept)
            group_short = title[len("tag_group:"):]
            for m, sections in kept.items():
                tag_to_groups[m].add(group_short)
                for section in sections:
                    if section:
                        tag_to_groups[m].add(f"{group_short}:{section}")
            progress(f"  [{i:>3}/{len(group_titles)}] {title:45s} {len(kept):>4} members "
                     f"({len(members)-len(kept)} unknown skipped)")

        spent = time.time() - t0
        if spent < delay:
            time.sleep(delay - spent)

    elapsed = time.time() - start
    progress(f"\nElapsed: {elapsed:.1f}s. Total non-tag references skipped: {skipped_unknown_total}")
    return dict(tag_to_groups)


def update_csv(csv_path: Path, tag_to_groups: dict[str, set[str]], out_path: Path):
    """Read csv_path, add a 'grouping' column between count and definition, write to out_path."""
    rows_in = []
    with open(csv_path, encoding="utf-8", newline="") as f:
        rows_in = list(csv.DictReader(f))

    new_fields = ["tag", "count", "grouping", "definition"]
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=new_fields)
        w.writeheader()
        n_with_group = 0
        for row in rows_in:
            tag = (row.get("tag") or "").strip()
            grps = tag_to_groups.get(tag, set())
            if grps:
                n_with_group += 1
            w.writerow({
                "tag": tag,
                "count": row.get("count", ""),
                "grouping": "|".join(sorted(grps)),
                "definition": row.get("definition", ""),
            })
    print(f"\nWrote {len(rows_in):,} rows to {out_path}")
    print(f"Tags with >=1 group: {n_with_group:,} ({n_with_group/len(rows_in)*100:.1f}%)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv",  type=Path, default=DEFAULT_TAGS_CSV,
                   help="Existing tag CSV to read (and base the output on).")
    p.add_argument("--out",  type=Path, default=None,
                   help="Output CSV (default: overwrite input).")
    p.add_argument("--rate", type=float, default=5.0)
    p.add_argument("--profile", default="chrome119")
    args = p.parse_args()

    if not args.csv.is_file():
        print(f"ERROR: {args.csv} not found", file=sys.stderr)
        return 1

    out_path = args.out or args.csv
    print(f"Reading existing tags from {args.csv}")
    known = load_known_tags(args.csv)
    print(f"  {len(known):,} known tags\n")

    session = cf_requests.Session(impersonate=args.profile)

    print("Listing all tag_group:* wiki pages...")
    groups = list_tag_groups(session)
    print(f"  {len(groups)} tag groups\n")

    print("Fetching each tag_group page and parsing members...")
    tag_to_groups = build_tag_to_groups(session, groups, known, rate_per_sec=args.rate)

    print(f"\n--- summary ---")
    print(f"groups scraped:   {len(groups)}")
    print(f"tags w/ groups:   {len(tag_to_groups):,}")
    # Show distribution
    sizes = sorted(((g, sum(1 for grps in tag_to_groups.values() if g in grps))
                    for g in {x for s in tag_to_groups.values() for x in s}),
                   key=lambda kv: -kv[1])
    print(f"\nTop 20 groups by member count:")
    for g, n in sizes[:20]:
        print(f"  {n:>5}  {g}")

    print(f"\nUpdating {out_path}")
    if out_path == args.csv:
        # Write to temp first, swap, to be safe
        tmp = args.csv.with_suffix(".csv.tmp")
        update_csv(args.csv, tag_to_groups, tmp)
        args.csv.unlink()
        tmp.rename(args.csv)
    else:
        update_csv(args.csv, tag_to_groups, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
