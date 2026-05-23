"""Safe conversion between Danbooru tag forms.

Danbooru stores tags with underscores (`looking_at_viewer`). For display and
for prompting most anime models, underscores are conventionally swapped for
spaces. However, a minority of Danbooru tags are *emoticons* in which the
underscore is part of the tag itself and must NOT be touched:

    =_=   >_<   o_o   ^_^   ;_;   ._.   0_0   -_-   @_@   x_x   *_*   +_+   t_t

Blindly running ``tag.replace("_", " ")`` mangles those. The heuristic here:
if the tag has any run of 2+ consecutive ASCII letters, it is word-form and
gets underscore→space; otherwise it is treated as an emoticon and the
underscores stay put.
"""

from __future__ import annotations

import csv
import re

_HAS_WORDY = re.compile(r"[A-Za-z]{2,}")

# Balanced ``(...)`` group whose opening paren is NOT already escaped.
# ``[^()]*`` keeps the match flat; danbooru qualifiers are never nested.
_PAREN_GROUP = re.compile(r"(?<!\\)\(([^()]*)\)")

# An already-escaped ``\(...\)`` region in an in-flight string. Used to
# carve a partially-rewritten tag into escaped vs. raw segments so we can
# apply the underscore→space rule ONLY to the raw segments.
_ESCAPED_REGION = re.compile(r"(\\\([^()]*\\\))")

# A backslash-escaped paren (``\(`` or ``\)``) as produced by
# ``escape_for_comfy`` or stored pre-escaped in the DB trigger field.
_ESCAPED_PAREN = re.compile(r"\\([()])")

# A tag stem plus its balanced ``(...)`` qualifier — ``fate_(series)`` or the
# display form ``fate (series)``. group(1) is the stem: it runs back to the
# previous *hard* delimiter (another paren/bracket, a ``:`` weight marker, or a
# comma) but is allowed to contain spaces, so multi-word series qualifiers like
# ``mahou shoujo madoka magica (anime)`` are captured whole while a hand-written
# weight wrapper ``(@artist:1.0)`` is NOT swallowed into the stem. group(2) is
# the qualifier text. The ``(?<!\\)`` before ``\(`` keeps an already-escaped
# ``\(`` from re-matching, so the membership-gated escape is idempotent.
_TAG_WITH_PAREN = re.compile(r"([^()\[\]:,\\]*)(?<!\\)\(([^()]*)\)")

# Collapse any run of whitespace to a single underscore (membership-key build).
_WS_RUN = re.compile(r"\s+")


def to_display_tag(tag: str) -> str:
    """Render a DB tag in display/prompt form.

    Word-form tags (anything containing 2+ consecutive ASCII letters) get
    underscores swapped for spaces. Emoticon-shaped tags (``=_=``, ``o_o``,
    ``;_;`` …) are left intact. Output is always lowercased.
    """
    s = tag.lower()
    if _HAS_WORDY.search(s):
        return s.replace("_", " ")
    return s


def escape_for_comfy(tag: str, known: set[str] | None = None) -> str:
    """Rewrite a single tag into ComfyUI-prompt-safe display form.

    ComfyUI's prompt parser treats unescaped parens as weight syntax
    (``(tag:1.2)``). Danbooru qualifiers embed literal parens —
    ``fate_(series)``, ``admiral_(kancolle)``, ``hammer_(sunset_beach)``
    — that silently mangle weights on neighbouring fragments if fed in raw.

    Escaping behaviour depends on ``known``:

    * ``known is None`` (legacy) — escape EVERY balanced ``(...)`` group. Kept
      for backward compatibility; callers that can author their own weight
      syntax should pass a ``known`` set instead.
    * ``known`` is a set — escape a ``(...)`` group ONLY when the tag it
      belongs to (stem + qualifier, normalized via :func:`_norm_paren_tag`) is
      a member, i.e. a real Danbooru/Gelbooru paren-tag. Build the set with
      :func:`load_known_paren_tags`. Tokens whose parens are NOT a known
      qualifier — a hand-written weight wrapper like ``(@artist:1.0)`` — are
      passed through byte-for-byte, including their underscores. A token with
      no parens at all still gets the emoticon-aware display rule
      (``looking_at_viewer`` → ``looking at viewer``; ``o_o`` stays ``o_o``).

    For an escaped group the joining underscore in front (if any) becomes a
    space and underscores inside the qualifier become spaces, so
    ``fate_(series)`` → ``fate \\(series\\)``.

    Idempotent either way: an already-escaped ``\\(`` is skipped via a negative
    lookbehind, so chaining the node after a previous escape pass is safe.
    """
    if not tag:
        return ""

    if known is None:
        def _repl_all(m: re.Match) -> str:
            inner = to_display_tag(m.group(1)).strip()
            return f" \\({inner}\\)"

        rewritten = _PAREN_GROUP.sub(_repl_all, tag)
        # Drop the joiner underscore(s) that used to attach the qualifier:
        # ``hammer_ \(sunset beach\)`` → ``hammer \(sunset beach\)``.
        rewritten = re.sub(r"_+ ", " ", rewritten)
        # Apply display-form to segments outside the escaped paren regions.
        parts = _ESCAPED_REGION.split(rewritten)
        parts = [p if p.startswith("\\(") else to_display_tag(p) for p in parts]
        out = "".join(parts)
        return re.sub(r" {2,}", " ", out).strip()

    # Membership-gated path. A token with no '(' can't carry a qualifier, so
    # just apply the display rule (and leave true emoticons intact).
    if "(" not in tag:
        return to_display_tag(tag).strip()

    def _repl_known(m: re.Match) -> str:
        # Not a real Danbooru/Gelbooru paren-tag → leave this group (and its
        # stem) exactly as written; it's the caller's own prompt syntax.
        if _norm_paren_tag(m.group(0)) not in known:
            return m.group(0)
        stem = to_display_tag(m.group(1)).strip()
        inner = to_display_tag(m.group(2)).strip()
        return f"{stem} \\({inner}\\)" if stem else f"\\({inner}\\)"

    out = _TAG_WITH_PAREN.sub(_repl_known, tag)
    return re.sub(r" {2,}", " ", out).strip()


def _norm_paren_tag(s: str) -> str:
    """Canonical membership key for a paren-bearing tag.

    Lowercase, whitespace→single underscore, collapsed underscore runs. Applied
    identically to the CSV names and to candidates pulled out of node input so
    that display form (``fate (series)``) and underscore form (``fate_(series)``)
    both resolve to the stored Danbooru key ``fate_(series)``.
    """
    s = _WS_RUN.sub("_", s.strip().lower())
    return re.sub(r"_+", "_", s)


_KNOWN_PAREN_TAGS: set[str] | None = None


def load_known_paren_tags() -> set[str]:
    """Set of Danbooru/Gelbooru tags that legitimately embed parens.

    Sourced from ``csv/danbooru_tags.csv`` (the raw ``name`` column) plus BOTH
    columns of ``csv/gelbooru_overrides.csv`` — so Gelbooru-preferred forms such
    as ``masturbation_(female)`` that may not appear in the raw Danbooru list
    are still recognized regardless of whether the swap ran first. Only
    paren-bearing names are kept, each normalized with :func:`_norm_paren_tag`.
    Cached for the life of the process; returns an empty set (escape nothing) if
    the CSVs are absent.
    """
    global _KNOWN_PAREN_TAGS
    if _KNOWN_PAREN_TAGS is not None:
        return _KNOWN_PAREN_TAGS
    from . import db as dblayer  # lazy: avoids import cost/cycles at module load

    known: set[str] = set()
    raw_csv = dblayer.DEFAULT_RAW_TAGS_CSV
    if raw_csv.exists():
        with open(raw_csv, "r", encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            next(reader, None)  # header row: name,post_count
            for row in reader:
                if row and "(" in row[0] and ")" in row[0]:
                    known.add(_norm_paren_tag(row[0]))
    overrides_csv = dblayer.PACK_DIR / "csv" / "gelbooru_overrides.csv"
    if overrides_csv.exists():
        with open(overrides_csv, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                for col in ("danbooru_form", "gelbooru_form"):
                    val = row.get(col) or ""
                    if "(" in val and ")" in val:
                        known.add(_norm_paren_tag(val))
    _KNOWN_PAREN_TAGS = known
    return known


def unescape_parens(tag: str) -> str:
    """Strip ComfyUI paren-escaping: ``fate \\(series\\)`` → ``fate (series)``.

    Inverse of the paren half of :func:`escape_for_comfy`. The DB stores
    character/artist triggers pre-escaped (``artoria pendragon \\(fate\\)``);
    consumers that re-apply their own escaping downstream want the raw form.
    Tags with no escaped parens pass through unchanged.
    """
    return _ESCAPED_PAREN.sub(r"\1", tag)


def split_character_trigger(trigger: str) -> tuple[str, str]:
    """Split a character `trigger` from the DB into (character_display, series_display).

    The CSV stores trigger as e.g. ``"hatsune miku, vocaloid"`` — first chunk
    is the character display name, second is the series. Returns ("", "") if
    the trigger is empty, or (name, "") if there is no series segment.
    """
    if not trigger:
        return "", ""
    parts = [p.strip() for p in trigger.split(",", 1)]
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]
