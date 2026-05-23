"""ComfyTagEscape: backslash-escape the literal parens in *recognized*
Danbooru/Gelbooru tags so ComfyUI's prompt parser treats them as text.

ComfyUI's prompt parser treats unescaped parens as weight syntax
(``(tag:1.2)``). Danbooru tags routinely embed literal parens for
qualifiers: ``fate_(series)``, ``admiral_(kancolle)``,
``hammer_(sunset_beach)``. Feeding those through CLIPTextEncode raw
breaks parsing and silently mangles weights on neighbouring fragments.

Crucially, escaping fires ONLY for parens that belong to a tag actually
present in ``csv/danbooru_tags.csv`` or ``csv/gelbooru_overrides.csv``.
A paren you wrote yourself — a weight wrapper like ``(@artist_name:1.0)``
— is NOT a Danbooru tag, so it is left exactly as typed. This is the
whole point of the node: it is not a blanket "escape every ``(``" pass.

For a recognized tag the qualifier becomes `` \\(...\\)`` with the joining
underscore dropped and inner underscores turned to spaces (``fate_(series)``
→ ``fate \\(series\\)``). Tokens with no parens still get the emoticon-aware
display rule (``looking_at_viewer`` → ``looking at viewer``; ``o_o`` and
``:)`` pass through).

Works on raw danbooru tags, gelbooru-swapped tags, character/artist tags
from the extractor — anything emitting comma- or newline-separated tag
strings. Newlines and commas are preserved so per-character outputs flow
cleanly. The escape is idempotent, so chaining this node after
GelbooruTagSwap (which now applies the same gated escape) is safe.
"""
from __future__ import annotations

from ..core.tagfmt import escape_for_comfy, load_known_paren_tags


class ComfyTagEscape:
    """Backslash-escape the parens of *recognized* Danbooru/Gelbooru tags
    (``fate_(series)`` → ``fate \\(series\\)``) so ComfyUI reads them as
    literal text. Parens that aren't part of a known tag — e.g. a
    hand-written ``(tag:1.2)`` weight wrapper — are left untouched. Accepts
    comma/newline separated tags in either underscore or space form."""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "tags": ("STRING", {"default": "", "multiline": True}),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("tags",)
    FUNCTION = "run"
    CATEGORY = "🎨 danbooru-tsc"

    def run(self, tags: str):
        if not tags:
            return ("",)
        known = load_known_paren_tags()
        out_lines: list[str] = []
        for line in tags.split("\n"):
            out_tokens: list[str] = []
            for raw in line.split(","):
                tok = raw.strip()
                if not tok:
                    continue
                out_tokens.append(escape_for_comfy(tok, known))
            out_lines.append(", ".join(out_tokens))
        return ("\n".join(out_lines),)
