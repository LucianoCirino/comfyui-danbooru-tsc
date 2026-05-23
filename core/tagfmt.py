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


def escape_for_comfy(tag: str) -> str:
    """Rewrite a single tag into ComfyUI-prompt-safe display form.

    ComfyUI's prompt parser treats unescaped parens as weight syntax
    (``(tag:1.2)``). Danbooru qualifiers embed literal parens —
    ``fate_(series)``, ``admiral_(kancolle)``, ``hammer_(sunset_beach)``
    — that silently mangle weights on neighbouring fragments if fed in raw.

    Each balanced ``(...)`` group is rewritten as `` \\(...\\)``: the
    joining underscore in front (if any) becomes a space, and underscores
    *inside* the qualifier are converted to spaces unconditionally
    (qualifiers are always word-form). Tag fragments OUTSIDE any paren
    group still go through the emoticon-aware display rule, so word-form
    tags get ``_``→space while true emoticons (``o_o``, ``=_=``, ``;_;``)
    keep their underscores. Lone parens that don't form a balanced pair
    (the ``)`` in ``:)``) are left alone — the rule only fires on grouped
    qualifiers, matching the convention Danbooru actually uses.

    Idempotent: already-escaped input passes through unchanged because the
    paren-group regex skips ``\\(`` via a negative lookbehind.
    """
    def _repl(m: re.Match) -> str:
        inner = to_display_tag(m.group(1)).strip()
        return f" \\({inner}\\)"

    rewritten = _PAREN_GROUP.sub(_repl, tag)
    # Drop the joiner underscore(s) that used to attach the qualifier:
    # ``hammer_ \(sunset beach\)`` → ``hammer \(sunset beach\)``.
    rewritten = re.sub(r"_+ ", " ", rewritten)

    # Apply display-form to segments outside the escaped paren regions.
    parts = _ESCAPED_REGION.split(rewritten)
    parts = [p if p.startswith("\\(") else to_display_tag(p) for p in parts]
    out = "".join(parts)

    return re.sub(r" {2,}", " ", out).strip()


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
