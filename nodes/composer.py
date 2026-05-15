"""AnimaPromptComposer: assemble Anima-format positive + negative prompts.

Anima's recommended structure:
  [quality/meta] [character_count] [character] [series] [@artist] [general_tags]

- spaces, not underscores, except score_X
- artist tag prefixed with @
- comma-separated, lowercase

Example final prompt:
  masterpiece, best quality, score_7, safe, 1girl, hatsune miku, vocaloid,
  @ebifurya, sitting on bench, looking at viewer, blue dress, sunset
"""

from __future__ import annotations

import re

from ..core.tagfmt import to_display_tag

CHAR_COUNTS = [
    "auto", "1girl", "1boy", "1other",
    "2girls", "2boys", "multiple girls", "multiple boys", "no humans",
]

QUALITY_PREFIX_PRESETS = {
    "default":      "masterpiece, best quality, score_7, safe",
    "high":         "masterpiece, best quality, score_8, safe",
    "very high":    "masterpiece, best quality, score_9, safe",
    "year-tagged":  "year 2025, newest, masterpiece, best quality, score_7, safe",
    "(custom)":     "",
}

NEGATIVE_PRESETS = {
    "default": "worst quality, low quality, score_1, score_2, score_3, artist name",
    "lite":    "worst quality, low quality",
    "(custom)": "",
}


def _norm(s: str) -> str:
    """lowercase, trimmed, with underscore→space only on word-form tags
    (emoticons like ``=_=`` keep their underscores)."""
    return to_display_tag(s.strip())


def _norm_keep_score(s: str) -> str:
    """Like _norm, but preserve score_N tokens (Anima requires those underscored)."""
    parts = []
    for tok in re.split(r"\s*,\s*", s):
        t = tok.strip()
        if not t:
            continue
        # Keep score_N as-is
        if re.match(r"^score_\d+$", t.lower()):
            parts.append(t.lower())
        else:
            parts.append(to_display_tag(t))
    return ", ".join(parts)


def _split_csv(s: str) -> list[str]:
    if not s:
        return []
    out = []
    for chunk in re.split(r"[,\n]+", s):
        c = chunk.strip()
        if c:
            out.append(c)
    return out


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in items:
        k = x.lower()
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def _count_from_prose(prose: str) -> str:
    """Lightweight heuristic for character_count when 'auto'."""
    p = (prose or "").lower()
    if not p:
        return "1girl"
    if re.search(r"\b(no one|empty|landscape only|scenery only|no humans?)\b", p):
        return "no humans"
    boys = len(re.findall(r"\b(boy|man|male|guy)s?\b", p))
    girls = len(re.findall(r"\b(girl|woman|female|lady)s?\b", p))
    if girls >= 2 and girls > boys:
        return "multiple girls"
    if boys >= 2 and boys > girls:
        return "multiple boys"
    if boys > girls:
        return "1boy"
    return "1girl"


class AnimaPromptComposer:
    """Compose the final Anima-format positive + negative prompt strings."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "character_trigger": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "From Danbooru Agent. e.g. 'hatsune miku, vocaloid'. Can be empty.",
                }),
                "artist_trigger": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "From Danbooru Agent. e.g. 'ebifurya'. Will be prefixed with @. Can be empty.",
                }),
                "general_tags": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "From Tag Refiner. Comma-separated.",
                }),
                "character_count": (CHAR_COUNTS, {"default": "auto"}),
                "quality_prefix_preset": (list(QUALITY_PREFIX_PRESETS.keys()), {"default": "default"}),
                "negative_preset": (list(NEGATIVE_PRESETS.keys()), {"default": "default"}),
            },
            "optional": {
                "extra_tags": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Optional extra tags from Danbooru Agent (mood, composition).",
                }),
                "enhanced_prose": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Used only when character_count is 'auto', to guess girl/boy count.",
                }),
                "quality_prefix_custom": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "Used when quality_prefix_preset == '(custom)'.",
                }),
                "negative_custom": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "tooltip": "Used when negative_preset == '(custom)'.",
                }),
                "append_prose": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "If True, append the enhanced_prose after the tag list (Anima's mixed mode).",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("positive_prompt", "negative_prompt")
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, character_trigger, artist_trigger, general_tags,
            character_count, quality_prefix_preset, negative_preset,
            extra_tags="", enhanced_prose="",
            quality_prefix_custom="", negative_custom="",
            append_prose=False):

        # Quality prefix
        if quality_prefix_preset == "(custom)":
            qp = quality_prefix_custom or ""
        else:
            qp = QUALITY_PREFIX_PRESETS.get(quality_prefix_preset, "")

        # Character count
        if character_count == "auto":
            cc = _count_from_prose(enhanced_prose)
        else:
            cc = character_count

        # Build positive in Anima's order:
        # [quality] [count] [character] [@artist] [extra] [general]
        parts: list[str] = []

        if qp.strip():
            parts.append(_norm_keep_score(qp))
        parts.append(cc)

        if character_trigger and character_trigger.strip():
            parts.append(_norm(character_trigger))

        if artist_trigger and artist_trigger.strip():
            artist = _norm(artist_trigger)
            # Don't double-prefix
            if not artist.startswith("@"):
                artist = f"@{artist}"
            parts.append(artist)

        # Combine extra_tags + general_tags into one bag, dedupe
        bag: list[str] = []
        for src in (extra_tags, general_tags):
            for t in _split_csv(src):
                bag.append(_norm(t))
        bag = _dedupe_preserve_order(bag)
        if bag:
            parts.append(", ".join(bag))

        positive = ", ".join(p for p in parts if p)

        if append_prose and enhanced_prose and enhanced_prose.strip():
            positive = positive + ". " + enhanced_prose.strip()

        # Negative
        if negative_preset == "(custom)":
            negative = negative_custom or ""
        else:
            negative = NEGATIVE_PRESETS.get(negative_preset, "")

        return (positive, negative)
