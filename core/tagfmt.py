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
