"""RandomTagSampler: pick N random tags from one tag_group, with a knob for
how much popularity (post_count) skews the probability.

popularity_weight semantics:
   0.0  -> uniform random (every tag equally likely)
   1.0  -> linear in post_count (a tag with 2x more posts is 2x more likely)
   2.0  -> strongly favor popular (count^2)
   -1.0 -> favor RARE tags (deep cuts, 1/count)
"""

from __future__ import annotations

import math
import random
import re

from ..core import db as dblayer
from ..core.tagfmt import to_display_tag


# ---------------------------------------------------------------------------
# Group dropdown helper
# ---------------------------------------------------------------------------

_FALLBACK_GROUPS = ["(rebuild DB first)"]


def _list_groups() -> list[str]:
    """Distinct grouping names, alphabetized. Used to populate the dropdown."""
    if not dblayer.db_exists():
        return list(_FALLBACK_GROUPS)
    try:
        conn = dblayer.connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT grouping FROM tag_groupings ORDER BY grouping"
            ).fetchall()
        finally:
            conn.close()
        groups = [r["grouping"] for r in rows if r["grouping"]]
        return groups or list(_FALLBACK_GROUPS)
    except Exception:
        return list(_FALLBACK_GROUPS)


# ---------------------------------------------------------------------------
# Weighted sampling without replacement (Efraimidis-Spirakis)
# ---------------------------------------------------------------------------

def _weighted_sample_no_replace(items, weights, k, rng):
    """Pick k items without replacement, biased by weights.

    Uses keys[i] = log(uniform()) / weight[i]; the k largest keys are picked.
    Handles weights == 0 by giving them effectively zero pickability.
    """
    if k >= len(items):
        # Just shuffle and return everything
        idxs = list(range(len(items)))
        rng.shuffle(idxs)
        return [items[i] for i in idxs]

    EPS = 1e-12
    scored = []
    for it, w in zip(items, weights):
        if w <= 0:
            continue
        u = rng.random()
        if u <= 0.0:
            u = EPS
        key = math.log(u) / w
        scored.append((key, it))
    # Largest keys get picked
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return [it for _, it in scored[:k]]


# ---------------------------------------------------------------------------
# Tag normalization for output
# ---------------------------------------------------------------------------

def _to_anima(tag: str) -> str:
    """Anima format: lowercased, with underscore→space conversion only on
    word-form tags. Emoticon tags like ``=_=`` or ``o_o`` keep underscores."""
    return to_display_tag(tag)


def _to_db_key(s: str) -> str:
    """Match-form for the DB: lowercase, underscores (not spaces)."""
    return s.strip().lower().replace(" ", "_")


def _parse_banned(raw: str) -> set[str]:
    """Parse banned_tags input into a set of underscore_form keys."""
    if not raw:
        return set()
    out = set()
    for chunk in re.split(r"[,\n;]+", raw):
        k = _to_db_key(chunk)
        if k:
            out.add(k)
    return out


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_HARD_CAP = 200  # 97.7% of first sentences fit under this; safety net for outliers.


def _short_def(text: str) -> str:
    """Return the first sentence of a definition, with a 200-char safety cap."""
    if not text:
        return ""
    text = text.strip()
    first = _SENTENCE_SPLIT.split(text, maxsplit=1)[0].strip()
    if len(first) <= _HARD_CAP:
        return first
    return text[:_HARD_CAP].rstrip().rstrip(",") + "..."


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

# Order matters: this is the dropdown order users see.
# Definitions use an em-dash (" — ") as the tag/def separator: parens collide
# with tags that contain them (e.g. "hatsune miku (cosplay)").
FORMAT_OPTIONS = [
    "tags",                      # blush, smile, looking at viewer
    "labeled",                   # face_tags: blush, smile, looking at viewer
    "group_only",                # face_tags
    "inline_with_defs",          # blush — rosy cheeks, smile — happy expression
    "labeled_inline_with_defs",  # face_tags: blush — rosy cheeks, smile — happy expression
    "newline",                   # one tag per line, no group, no defs
    "newline_with_defs",         # one "tag — def" per line, no group
    "yaml_with_defs",            # multi-line YAML: group header + "- tag: def" lines
]


# Separator between tag and its definition. Chosen because em-dashes do not
# appear in danbooru tags and are rare in the first sentence of definitions,
# so the output stays unambiguously parseable by " — ".
_DEF_SEP = " — "


def _format_output(group: str, picked_tags: list[str],
                   tag_to_def: dict, fmt: str) -> tuple[str, int]:
    """Render picked tags in the requested format. Returns (text, n_with_def)."""
    if fmt == "group_only":
        return (group if picked_tags else "", 0)

    if not picked_tags:
        return ("", 0)

    anima = [_to_anima(t) for t in picked_tags]
    defs = [_short_def(tag_to_def.get(t, "")) for t in picked_tags]
    n_with_def = sum(1 for d in defs if d)

    if fmt == "tags":
        return (", ".join(anima), n_with_def)

    if fmt == "labeled":
        return (f"{group}: " + ", ".join(anima), n_with_def)

    if fmt == "newline":
        return ("\n".join(anima), n_with_def)

    if fmt == "inline_with_defs":
        parts = [f"{a}{_DEF_SEP}{d}" if d else a for a, d in zip(anima, defs)]
        return (", ".join(parts), n_with_def)

    if fmt == "labeled_inline_with_defs":
        parts = [f"{a}{_DEF_SEP}{d}" if d else a for a, d in zip(anima, defs)]
        return (f"{group}: " + ", ".join(parts), n_with_def)

    if fmt == "newline_with_defs":
        parts = [f"{a}{_DEF_SEP}{d}" if d else a for a, d in zip(anima, defs)]
        return ("\n".join(parts), n_with_def)

    if fmt == "yaml_with_defs":
        lines = [f"{group}:"]
        for a, d in zip(anima, defs):
            lines.append(f"  - {a}: {d}" if d else f"  - {a}")
        return ("\n".join(lines), n_with_def)

    # Unknown format — fall back to plain.
    return (", ".join(anima), n_with_def)


def _format_group_listing(rows, picked_set: set | None = None,
                          probs: list[float] | None = None) -> str:
    """Return one-line-per-tag listing of `rows`.

    When `probs` is provided (single-draw pick probability per row, same order
    as `rows`), the listing is sorted by probability desc — so whichever tags
    the current popularity_weight favors float to the top — and each row shows
    its percentage. With no probs, falls back to count desc.

    Tags in `picked_set` are marked with ' [*]'.
    """
    if probs is not None:
        # Sort by probability desc, count desc as tiebreaker.
        paired = sorted(
            zip(rows, probs),
            key=lambda rp: (-rp[1], -rp[0]["count"]),
        )
    else:
        paired = [(r, None) for r in sorted(rows, key=lambda r: r["count"], reverse=True)]

    lines = []
    for r, p in paired:
        mark = " [*]" if picked_set and r["tag"] in picked_set else ""
        if p is not None:
            lines.append(f"  - {r['tag']}: {r['count']:,}  ({p * 100:6.3f}%){mark}")
        else:
            lines.append(f"  - {r['tag']}: {r['count']:,}{mark}")
    return "\n".join(lines)


def _compute_weights_and_probs(counts: list[int], popularity_weight: float):
    """Same weight formula used by the sampler. Returns (weights, probs)."""
    if popularity_weight == 0.0:
        weights = [1.0] * len(counts)
    else:
        weights = [max(c, 1) ** popularity_weight for c in counts]
    total = sum(weights)
    probs = [w / total for w in weights] if total > 0 else [0.0] * len(weights)
    return weights, probs


# ---------------------------------------------------------------------------
# ComfyUI node
# ---------------------------------------------------------------------------

class RandomTagSampler:
    """Pick N random tags from one Danbooru tag group."""

    @classmethod
    def INPUT_TYPES(cls):
        groups = _list_groups()
        # If the user has a built DB, default to a sensible group
        default_group = "face_tags" if "face_tags" in groups else groups[0]
        return {
            "required": {
                "group": (groups, {"default": default_group}),
                "count": ("INT", {"default": 5, "min": 1, "max": 100}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "output_format": (FORMAT_OPTIONS, {
                    "default": "tags",
                    "tooltip": (
                        "How the picked tags are rendered:\n"
                        "  tags                     - blush, smile\n"
                        "  labeled                  - face_tags: blush, smile\n"
                        "  group_only               - face_tags\n"
                        "  inline_with_defs         - blush — rosy cheeks, smile — happy\n"
                        "  labeled_inline_with_defs - face_tags: blush — rosy cheeks, ...\n"
                        "  newline                  - one tag per line\n"
                        "  newline_with_defs        - one 'tag — def' per line\n"
                        "  yaml_with_defs           - multi-line YAML with group header"
                    ),
                }),
            },
            "optional": {
                "popularity_weight": ("FLOAT", {
                    "default": 0.5, "min": -3.0, "max": 5.0, "step": 0.1,
                    "tooltip": "0=uniform, 1=linear with count, 2+=favor popular, negative=favor rare.",
                }),
                "min_post_count": ("INT", {
                    "default": 0, "min": 0, "max": 10000000,
                    "tooltip": "Skip tags below this post_count. 0 = no filter.",
                }),
                "banned_tags": ("STRING", {
                    "default": "",
                    "multiline": True,
                    "tooltip": "Comma- or newline-separated tags to NEVER pick. Either form works (e.g. 'turn_pale' or 'turn pale').",
                }),
                "output_chance": ("FLOAT", {
                    "default": 1.0, "min": 0.0, "max": 1.0, "step": 0.01,
                    "tooltip": "Probability of emitting any output. 1.0=always, 0.0=never. On a miss, all string outputs (including group) are empty.",
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("tags", "debug_info")
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, group, count, seed, output_format="tags",
            popularity_weight=0.5, min_post_count=0, banned_tags="",
            output_chance=1.0):

        if not dblayer.db_exists():
            err = "danbooru.db not found. Run 'Danbooru DB Build' first."
            return ("", err)

        if group.startswith("("):
            err = f"Invalid group selection: {group}"
            return ("", err)

        banned = _parse_banned(banned_tags)
        rng = random.Random(seed)

        # DB fetch happens before the chance gate so the skip-branch debug can
        # still report the full group contents. The chance roll is still the
        # first rng draw, so seed→roll mapping is unchanged.
        conn = dblayer.connect()
        try:
            rows = conn.execute(
                """
                SELECT t.tag, t.count, t.definition
                FROM tags t
                JOIN tag_groupings tg ON tg.tag_id = t.id
                WHERE tg.grouping = ? AND t.count >= ?
                """,
                (group, min_post_count),
            ).fetchall()
        finally:
            conn.close()

        n_raw = len(rows)

        # Apply banlist (DB stores underscore_form, banned set is also underscore_form)
        n_banned_hit = 0
        if banned:
            filtered = []
            for r in rows:
                if r["tag"] in banned:
                    n_banned_hit += 1
                else:
                    filtered.append(r)
            rows = filtered

        if not rows:
            return (
                "",
                f"=== RandomTagSampler ===\n"
                f"group              : {group}\n"
                f"raw members        : {n_raw} (before filters)\n"
                f"min_post_count     : {min_post_count}\n"
                f"banned tags hit    : {n_banned_hit} of {len(banned)} declared\n"
                f"\nNo tags left in group '{group}' after filters.",
            )

        # Compute weights/probabilities up front so both the skip and pass
        # branches can display per-tag pick probability.
        counts = [r["count"] for r in rows]
        weights, probs = _compute_weights_and_probs(counts, popularity_weight)

        # Output-chance gate
        chance_roll = rng.random()
        if chance_roll >= output_chance:
            debug = (
                f"=== RandomTagSampler ===\n"
                f"group              : {group}\n"
                f"output_format      : {output_format}\n"
                f"output_chance      : {output_chance}\n"
                f"roll               : {chance_roll:.4f} (>= chance, SKIPPED)\n"
                f"seed               : {seed}\n"
                f"raw members        : {n_raw} (before filters)\n"
                f"available members  : {len(rows)} (after filters)\n"
                f"min_post_count     : {min_post_count}\n"
                f"banned tags hit    : {n_banned_hit} of {len(banned)} declared\n"
                f"popularity_weight  : {popularity_weight}\n"
                f"\ngroup contents (tag: post_count (pick %), sorted by % desc):\n"
                f"{_format_group_listing(rows, probs=probs)}"
            )
            return ("", debug)

        # Map tag -> definition for the picked-tag lookup later
        tag_to_def = {r["tag"]: (r["definition"] or "") for r in rows}
        tags = [r["tag"] for r in rows]

        picked_tags = _weighted_sample_no_replace(tags, weights, count, rng)
        picked_set = set(picked_tags)
        out, n_with_def = _format_output(group, picked_tags, tag_to_def, output_format)

        debug = (
            f"=== RandomTagSampler ===\n"
            f"group              : {group}\n"
            f"output_format      : {output_format}\n"
            f"raw members        : {n_raw} (before filters)\n"
            f"available members  : {len(tags)} (after filters)\n"
            f"min_post_count     : {min_post_count}\n"
            f"banned tags hit    : {n_banned_hit} of {len(banned)} declared\n"
            f"popularity_weight  : {popularity_weight}\n"
            f"output_chance      : {output_chance} (roll {chance_roll:.4f}, passed)\n"
            f"requested count    : {count}\n"
            f"returned count     : {len(picked_tags)}\n"
            f"definitions found  : {n_with_def} of {len(picked_tags)}\n"
            f"seed               : {seed}\n"
            f"\ngroup contents (tag: post_count (pick %), sorted by % desc; [*] = picked):\n"
            f"{_format_group_listing(rows, picked_set=picked_set, probs=probs)}\n"
            f"\noutput:\n{out}"
        )
        return (out, debug)
