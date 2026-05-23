"""GelbooruTagSwap: rewrite Danbooru-canonical tags to Gelbooru-preferred
spellings using csv/gelbooru_overrides.csv.

Anima-family anime models follow the convention "when a tag differs between
Danbooru and Gelbooru, prefer the Gelbooru version." This node applies that
rule by looking each comma-separated token up in the override CSV and
substituting the Gelbooru form when a mapping exists. Tokens not in the
override list pass through unchanged. Newlines are preserved so the
per-character newline-separated outputs from the extractor flow cleanly.
"""
from __future__ import annotations

import csv

from ..core import db as dblayer
from ..core.tagfmt import to_display_tag, escape_for_comfy, load_known_paren_tags


_OVERRIDES_CACHE: dict[str, str] | None = None
_OVERRIDES_PATH = dblayer.PACK_DIR / "csv" / "gelbooru_overrides.csv"


def _load_overrides() -> dict[str, str]:
    """Map keyed by display-form (lowercase, underscores→spaces for word tags).

    The CSV stores tags in Danbooru-canonical underscore form, but the rest of
    the pipeline (refiner, extractor) emits display form (`hair between eyes`,
    not `hair_between_eyes`). Normalize both keys and values to display form
    on load so lookups match regardless of which form the caller hands us.
    """
    global _OVERRIDES_CACHE
    if _OVERRIDES_CACHE is not None:
        return _OVERRIDES_CACHE
    out: dict[str, str] = {}
    if _OVERRIDES_PATH.exists():
        with open(_OVERRIDES_PATH, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                dan = (row.get("danbooru_form") or "").strip()
                gel = (row.get("gelbooru_form") or "").strip()
                if dan and gel and dan != gel:
                    out[to_display_tag(dan)] = to_display_tag(gel)
    _OVERRIDES_CACHE = out
    return out


class GelbooruTagSwap:
    """Substitute Danbooru-canonical tags with Gelbooru-preferred spellings
    from csv/gelbooru_overrides.csv. Tokens not in the map pass through
    unchanged; newlines and comma-separated structure are preserved."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "tags": ("STRING", {"default": "", "multiline": True}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("tags", "swap_report")
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, tags: str):
        overrides = _load_overrides()
        if not tags:
            return ("", f"{len(overrides):,} overrides loaded, 0 applied (empty input)")
        if not overrides:
            return (tags, f"no overrides loaded — is {_OVERRIDES_PATH.name} present?")

        known = load_known_paren_tags()
        swaps: list[tuple[str, str]] = []
        out_lines: list[str] = []
        for line in tags.split("\n"):
            out_tokens: list[str] = []
            for raw in line.split(","):
                tok = raw.strip()
                if not tok:
                    continue
                # Normalize to display form for lookup. Accepts both
                # underscore and space input; emits display form.
                tok_key = to_display_tag(tok)
                replacement = overrides.get(tok_key)
                emitted = replacement if replacement else tok
                # Several overrides target paren-qualifier forms (e.g.
                # `female_masturbation` → `masturbation_(female)`), and
                # the input may already carry qualifier tags untouched.
                # Run every emitted token through the comfy-escape rule so
                # the output is safe for CLIPTextEncode either way. Gated on
                # the known-tag set so a pass-through weight wrapper isn't
                # mangled; idempotent on already-escaped input.
                emitted = escape_for_comfy(emitted, known)
                if replacement:
                    swaps.append((tok, emitted))
                out_tokens.append(emitted)
            out_lines.append(", ".join(out_tokens))

        report = [f"{len(overrides):,} overrides loaded, {len(swaps)} applied"]
        for old, new in swaps:
            report.append(f"  {old}  ->  {new}")
        return ("\n".join(out_lines), "\n".join(report))
