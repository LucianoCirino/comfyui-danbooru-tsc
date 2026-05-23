"""DanbooruTagAnnotator: given a list of danbooru tags, render each with its
definition in a configurable output format.

Mirror of the RandomTagSampler's output-format dropdown (minus the
labeled/group_only options, which assume a single shared group). Default is
`inline_with_defs` — the comma-separated `tag {definition}` form, since
the whole point of the node is feeding definitions into a downstream LLM.

The optional `include_group` toggle prefixes each definition with the
top-level group(s) the tag belongs to, e.g. `blush {[face_tags] rosy
cheeks}`. Unlike the sampler — which works within one chosen group — an
arbitrary input tag can belong to several groups, so all top-level groups
are listed.
"""
from __future__ import annotations

import re

from ..core import db as dblayer
from ..core.search import lookup_tags


_SPLIT_RE = re.compile(r"[,\n]+")


def _top_level_groups(grouping: str) -> list[str]:
    """Deduped top-level group names from the pipe-separated, hierarchical
    `tags.grouping` string. 'face_tags|face_tags:emotions' -> ['face_tags'];
    'eyes_tags|eyes_tags:gazes|posture' -> ['eyes_tags', 'posture']. Order is
    preserved by first appearance."""
    seen: set[str] = set()
    out: list[str] = []
    for part in grouping.split("|"):
        top = part.split(":", 1)[0].strip()
        if top and top not in seen:
            seen.add(top)
            out.append(top)
    return out


# Same dropdown shape as RandomTagSampler (minus labeled/group_only, which
# require a group concept). Definitions wrap in curly braces — no danbooru
# tag contains them, so the output stays unambiguously parseable.
FORMAT_OPTIONS = [
    "inline_with_defs",    # blush {rosy cheeks}, smile {happy expression}
    "tags",                # blush, smile, looking at viewer  (no defs)
    "newline",             # one tag per line, no defs
    "newline_with_defs",   # one "tag {def}" per line
    "yaml_with_defs",      # multi-line YAML-ish: "- tag: def"
]


def _with_def(tag: str, definition: str) -> str:
    """Render a tag with its definition wrapped in curly braces."""
    return f"{tag} {{{definition}}}" if definition else tag


def _render(picked_tags: list[str], tag_to_def: dict[str, str], fmt: str) -> str:
    """Render aligned tag/definition pairs in the requested format."""
    if not picked_tags:
        return ""
    defs = [tag_to_def.get(t, "") for t in picked_tags]

    if fmt == "tags":
        return ", ".join(picked_tags)
    if fmt == "newline":
        return "\n".join(picked_tags)
    if fmt == "inline_with_defs":
        return ", ".join(_with_def(t, d) for t, d in zip(picked_tags, defs))
    if fmt == "newline_with_defs":
        return "\n".join(_with_def(t, d) for t, d in zip(picked_tags, defs))
    if fmt == "yaml_with_defs":
        lines = []
        for t, d in zip(picked_tags, defs):
            lines.append(f"- {t}: {d}" if d else f"- {t}")
        return "\n".join(lines)
    # Unknown format → fall back to the default.
    return ", ".join(_with_def(t, d) for t, d in zip(picked_tags, defs))


class DanbooruTagAnnotator:
    """Look up each input tag's definition. Render in the chosen output
    format. Unknown tags surface in a separate `not_found` output."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "tags": ("STRING", {
                    "default": "1girl, looking at viewer, holding hands",
                    "multiline": True,
                    "tooltip": (
                        "Comma or newline separated danbooru tags. "
                        "Accepts either underscore or space form."
                    ),
                }),
                "output_format": (FORMAT_OPTIONS, {
                    "default": "inline_with_defs",
                    "tooltip": (
                        "How the tags + definitions are rendered:\n"
                        "  inline_with_defs   - blush {rosy cheeks}, smile {happy}\n"
                        "  tags               - blush, smile  (no defs)\n"
                        "  newline            - one tag per line\n"
                        "  newline_with_defs  - one 'tag {def}' per line\n"
                        "  yaml_with_defs     - '- tag: def' multi-line"
                    ),
                }),
            },
            "optional": {
                "max_definition_chars": ("INT", {
                    "default": 300, "min": 50, "max": 2000,
                    "tooltip": (
                        "Truncate each definition to this many chars to "
                        "keep LLM context manageable. Has no effect on "
                        "the `tags` and `newline` formats."
                    ),
                }),
                "include_group": ("BOOLEAN", {
                    "default": False,
                    "tooltip": (
                        "Prefix each definition with the top-level danbooru "
                        "group(s) the tag belongs to, e.g.\n"
                        "  blush {[face_tags] rosy cheeks}\n"
                        "A tag in several groups lists them all "
                        "([eyes_tags, posture, ...]). Has no effect on the "
                        "`tags` and `newline` formats (they drop definitions)."
                    ),
                }),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("annotated", "not_found")
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, tags: str, output_format: str = "inline_with_defs",
            max_definition_chars: int = 300, include_group: bool = False):
        if not dblayer.db_exists():
            err = (
                f"danbooru.db not found at {dblayer.DB_PATH}. "
                "Run the 'Danbooru DB Build' node once."
            )
            return ("", err)
        if not tags or not tags.strip():
            return ("", "")

        # Split on commas + newlines, dedupe preserving input order.
        seen: set[str] = set()
        ordered: list[str] = []
        for chunk in _SPLIT_RE.split(tags):
            t = chunk.strip()
            if t and t not in seen:
                seen.add(t)
                ordered.append(t)

        results = lookup_tags(ordered, include_grouping=include_group)

        # Build aligned (tag_display, definition) pairs and a not-found list.
        picked: list[str] = []
        tag_to_def: dict[str, str] = {}
        not_found: list[str] = []
        for inp, res in zip(ordered, results):
            if "definition" in res:
                tag_disp = res.get("tag") or inp
                defn = res["definition"] or ""
                if len(defn) > max_definition_chars:
                    defn = defn[:max_definition_chars] + "..."
                if include_group:
                    groups = _top_level_groups(res.get("grouping") or "")
                    if groups:
                        label = "[" + ", ".join(groups) + "]"
                        # Keep the group visible even when the tag has no
                        # textual definition (so the braces aren't dropped).
                        defn = f"{label} {defn}".rstrip() if defn else label
                picked.append(tag_disp)
                tag_to_def[tag_disp] = defn
            else:
                # Keep the input form for unknown tags so the output shape
                # still reflects the user's request. Definition is empty.
                picked.append(inp)
                tag_to_def[inp] = ""
                not_found.append(inp)

        return (_render(picked, tag_to_def, output_format), ", ".join(not_found))
